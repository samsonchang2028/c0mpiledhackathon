const params = new URLSearchParams(location.search);
const version = params.get("version") === "v2" ? "v2" : "v1";
const workspace = document.querySelector("#workspace");
const workflowNav = document.querySelector("#workflow-nav");

document.querySelector("#active-version").textContent = version;
document.querySelector(`#version-${version}`).classList.add("active");

// One deterministic table changes both accessible labels and replay-facing IDs.
const copy = {
  v1: {
    bookingNav: ["booking-nav", "Book appointment"],
    quoteNav: ["quote-nav", "Request quote"],
    bookingTitle: "Book an appointment",
    name: ["customer-name", "Customer name"],
    service: ["service", "Service"],
    date: ["appointment-date", "Date"],
    time: ["appointment-time", "Time"],
    bookingSubmit: ["book-appointment", "Book appointment"],
    quoteTitle: "Request a quote",
    company: ["company", "Company"],
    category: ["request-category", "Request category"],
    notes: ["notes", "Notes"],
    quoteSubmit: ["request-quote", "Request quote"]
  },
  v2: {
    bookingNav: ["schedule-nav", "Schedule visit"],
    quoteNav: ["estimate-nav", "Get an estimate"],
    bookingTitle: "Schedule a service visit",
    name: ["visitor-name", "Client name"],
    service: ["visit-type", "Visit type"],
    date: ["visit-date", "Preferred day"],
    time: ["visit-window", "Arrival window"],
    bookingSubmit: ["schedule-visit", "Schedule visit"],
    quoteTitle: "Get a project estimate",
    company: ["organization", "Organization"],
    category: ["project-type", "Project type"],
    notes: ["project-details", "Project details"],
    quoteSubmit: ["send-estimate", "Send estimate request"]
  }
}[version];

function field([id, label], control) {
  return `<div class="field"><label for="${id}">${label}</label>${control(id)}</div>`;
}

function textInput(id) {
  return `<input id="${id}" name="${id}" autocomplete="name" required>`;
}

function serviceSelect(id) {
  return `<select id="${id}" name="${id}" required>
    <option value="">Choose a service</option>
    <option>Consultation</option>
    <option>Installation</option>
    <option>Maintenance</option>
  </select>`;
}

function dateInput(id) {
  return `<input id="${id}" name="${id}" type="date" required>`;
}

function timeSelect(id) {
  return `<select id="${id}" name="${id}" required>
    <option value="">Choose a time</option>
    <option value="09:00">9:00 AM</option>
    <option value="11:00">11:00 AM</option>
    <option value="14:00">2:00 PM</option>
    <option value="16:00">4:00 PM</option>
  </select>`;
}

function categorySelect(id) {
  return `<select id="${id}" name="${id}" required>
    <option value="">Choose a category</option>
    <option>New installation</option>
    <option>Upgrade</option>
    <option>Repair</option>
  </select>`;
}

function notesInput(id) {
  return `<textarea id="${id}" name="${id}" required></textarea>`;
}

function submitArea([id, label]) {
  const button = `<button id="${id}" type="submit">${label}</button>`;
  return version === "v2" ? `<div class="v2-actions">${button}</div>` : button;
}

function showBooking() {
  setActive("booking");
  workspace.innerHTML = `
    <div class="panel">
      <h2>${copy.bookingTitle}</h2>
      <p class="intro">Reserve a time for a customer service appointment.</p>
      <form id="booking-form">
        ${field(copy.name, textInput)}
        ${field(copy.service, serviceSelect)}
        <div class="field-row">
          ${field(copy.date, dateInput)}
          ${field(copy.time, timeSelect)}
        </div>
        ${submitArea(copy.bookingSubmit)}
      </form>
    </div>`;
  document.querySelector("#booking-form").addEventListener("submit", event => {
    event.preventDefault();
    workspace.innerHTML = `
      <div id="booking-confirmation" class="panel confirmation" role="status">
        <h2>Appointment confirmed</h2>
        <p>The service visit has been added to the schedule.</p>
      </div>`;
  });
}

function showQuote() {
  setActive("quote");
  workspace.innerHTML = `
    <div class="panel">
      <h2>${copy.quoteTitle}</h2>
      <p class="intro">Send project details to the estimating queue.</p>
      <form id="quote-form">
        ${field(copy.company, textInput)}
        ${field(copy.category, categorySelect)}
        ${field(copy.notes, notesInput)}
        ${submitArea(copy.quoteSubmit)}
      </form>
    </div>`;
  document.querySelector("#quote-form").addEventListener("submit", event => {
    event.preventDefault();
    workspace.innerHTML = `
      <div id="quote-confirmation" class="panel confirmation" role="status">
        <h2>Quote request received</h2>
        <p>The request is ready for the estimating team.</p>
      </div>`;
  });
}

function setActive(name) {
  workflowNav.querySelectorAll("button").forEach(button => {
    button.classList.toggle("active", button.dataset.workflow === name);
  });
}

function navButton([id, label], workflow) {
  return `<button id="${id}" data-workflow="${workflow}" type="button">${label}</button>`;
}

workflowNav.innerHTML =
  navButton(copy.bookingNav, "booking") +
  navButton(copy.quoteNav, "quote");
workflowNav.querySelector('[data-workflow="booking"]').addEventListener("click", showBooking);
workflowNav.querySelector('[data-workflow="quote"]').addEventListener("click", showQuote);

showBooking();
