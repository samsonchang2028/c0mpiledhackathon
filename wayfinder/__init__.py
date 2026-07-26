"""Wayfinder: a verified, continuously-updated agent-navigation layer.

The engine keeps a state-action graph per site. Nodes are identified by the set
of actions available in them; a separate DOM signature is used only to detect
drift. Edges carry a ranked locator ensemble, a mutation class that gates how
they may be verified, and an empirically calibrated confidence score.
"""

__version__ = "0.2.0"
