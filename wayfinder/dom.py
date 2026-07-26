"""Live page perception: interactive-element inventory and drift signatures.

Everything the healer knows about the *current* page comes from here. Nothing in
this module reads the stored graph, which is deliberate: the inventory has to be
an honest description of what is on screen right now, with no knowledge of what
we hoped to find.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Collect one record per interactive control, with enough structure for the
# healer to score candidates: semantic name, control type, option set, the
# region it lives in, and its ordinal position among its peers.
_INVENTORY_JS = """
() => {
  const accessibleName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(node => (node.innerText || node.textContent || '').trim());
      if (parts.length) return parts.join(' ').trim();
    }
    if (el.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (explicit) return (explicit.innerText || explicit.textContent || '').trim();
    }
    const wrapping = el.closest('label');
    if (wrapping) return (wrapping.innerText || wrapping.textContent || '').trim();
    const text = (el.innerText || el.textContent || '').trim();
    if (text) return text;
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim();
    return '';
  };

  const implicitRole = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'submit' || type === 'button') return 'button';
      return 'textbox';
    }
    return 'generic';
  };

  const cssPath = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      if (node.id) { parts.unshift(`${part}#${node.id}`); break; }
      const parent = node.parentElement;
      if (parent) {
        const peers = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (peers.length > 1) part += `:nth-of-type(${peers.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };

  const regionOf = (el) => {
    const container = el.closest('form, nav, aside, section, main, header, [role="region"]');
    if (!container) return { kind: 'document', id: null, label: null };
    return {
      kind: container.tagName.toLowerCase(),
      id: container.id || null,
      label: (container.getAttribute('aria-label') || '').trim() || null
    };
  };

  const visible = (el) => {
    if (el.hidden) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const nodes = Array.from(
    document.querySelectorAll('button, a[href], input, textarea, select, [role="button"], [role="link"], [role="tab"]')
  ).filter(visible).slice(0, 120);

  const regionCounters = {};
  return nodes.map((el, index) => {
    const tag = el.tagName.toLowerCase();
    const region = regionOf(el);
    const regionKey = `${region.kind}#${region.id || ''}`;
    const typeKey = `${regionKey}|${tag}|${(el.getAttribute('type') || '').toLowerCase()}`;
    regionCounters[typeKey] = (regionCounters[typeKey] || 0) + 1;

    return {
      index,
      tag,
      type: (el.getAttribute('type') || '').toLowerCase() || null,
      role: implicitRole(el),
      id: el.id || null,
      name: el.getAttribute('name') || null,
      test_id: el.getAttribute('data-testid') || null,
      accessible_name: accessibleName(el).slice(0, 160),
      placeholder: (el.getAttribute('placeholder') || '').trim() || null,
      text: (el.innerText || el.textContent || '').trim().slice(0, 160),
      options: tag === 'select'
        ? Array.from(el.options).map(o => ({
            value: o.value,
            label: (o.textContent || '').trim()
          })).slice(0, 40)
        : null,
      region,
      ordinal_in_region: regionCounters[typeKey] - 1,
      required: el.hasAttribute('required'),
      css_path: cssPath(el),
      disabled: el.disabled === true
    };
  });
}
"""

# The skeleton deliberately drops ids, classes, and all text. Those are exactly
# the things a redesign churns, and treating them as drift would make every
# copy edit invalidate the graph.
_SKELETON_JS = """
() => {
  const walk = (el, depth) => {
    if (depth > 8) return '';
    const tag = el.tagName.toLowerCase();
    if (['script', 'style', 'link', 'meta'].includes(tag)) return '';
    const role = el.getAttribute('role') || '';
    const type = el.getAttribute('type') || '';
    const self = role || type ? `${tag}[${role}${type ? ':' + type : ''}]` : tag;
    const children = Array.from(el.children)
      .map(child => walk(child, depth + 1))
      .filter(Boolean)
      .join(',');
    return children ? `${self}(${children})` : self;
  };
  return walk(document.body, 0);
}
"""

_LANDMARK_JS = """
() => Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"], nav, main, form'))
  .slice(0, 40)
  .map(el => {
    const tag = el.tagName.toLowerCase();
    if (['nav', 'main', 'form'].includes(tag)) return `${tag}#${el.id || ''}`;
    return `${tag}:${(el.innerText || el.textContent || '').trim().slice(0, 80)}`;
  })
"""


def capture_inventory(page: Any) -> list[dict[str, Any]]:
    """Every visible, interactive control on the page, with scoring context."""
    try:
        return page.evaluate(_INVENTORY_JS) or []
    except Exception:  # pragma: no cover - a dead page yields no candidates
        return []


def capture_signature(page: Any) -> dict[str, Any]:
    """A structural fingerprint used *only* to detect that a node has drifted."""
    try:
        skeleton = page.evaluate(_SKELETON_JS) or ""
    except Exception:  # pragma: no cover
        skeleton = ""
    try:
        landmarks = page.evaluate(_LANDMARK_JS) or []
    except Exception:  # pragma: no cover
        landmarks = []

    return {
        "skeleton_hash": _hash(skeleton),
        "landmark_hash": _hash(json.dumps(landmarks, sort_keys=True)),
        "landmarks": landmarks[:20],
        "element_count": skeleton.count("(") + skeleton.count(","),
    }


def signature_drift(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Compare two signatures. Skeleton change is structural drift (high signal);
    landmark-only change is usually a copy edit (low signal)."""
    if not old:
        return {"drifted": False, "kind": "no_baseline", "detail": "no stored signature"}

    skeleton_changed = old.get("skeleton_hash") != new.get("skeleton_hash")
    landmark_changed = old.get("landmark_hash") != new.get("landmark_hash")

    if skeleton_changed:
        return {"drifted": True, "kind": "structural", "detail": "DOM skeleton hash changed"}
    if landmark_changed:
        return {"drifted": True, "kind": "content", "detail": "landmark text changed"}
    return {"drifted": False, "kind": "stable", "detail": "signature unchanged"}


def observed_action_set(inventory: list[dict[str, Any]]) -> list[str]:
    """A coarse, markup-independent description of what can be done here.

    Used to check that we are still in the node we think we are in, independent
    of how the controls are labelled.
    """
    kinds = set()
    for element in inventory:
        role = element.get("role")
        if role == "button":
            kinds.add("submit" if element.get("type") == "submit" else "activate")
        elif role == "link":
            kinds.add("navigate")
        elif role == "combobox":
            kinds.add("choose")
        elif role == "textbox":
            kinds.add("enter_text")
    return sorted(kinds)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
