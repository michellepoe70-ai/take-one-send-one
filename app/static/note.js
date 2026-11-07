"use strict";

const form = document.querySelector("#note-form");
const fields = document.querySelector("#note-fields");
const status = document.querySelector("#status");
const saveButton = document.querySelector("#save-button");
const referenceInput = document.querySelector("#client-reference");
const dateInput = document.querySelector("#session-date");
const noteInput = document.querySelector("#note");
const viewsPanel = document.querySelector("#views-panel");
const generateButton = document.querySelector("#generate-button");
const reviewerInput = document.querySelector("#reviewer-name");
const generationStatus = document.querySelector("#generation-status");
let dirty = false;
let draftId = window.location.pathname.startsWith("/notes/")
  ? window.location.pathname.split("/")[2] : null;

function showStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle("error", error);
}

async function responseData(response) {
  const body = await response.json();
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "Check the fields and try again.");
  }
  return body;
}

function setEditTitle() {
  document.querySelector("#page-title").textContent = "Edit draft";
  document.title = "Edit draft | Take One, Send One";
}

const reasonText = {
  client_list_by_diagnosis_requested: "client list by diagnosis requested",
  identifier_in_funder_view: "identifier found in the funder view",
  unexpected_or_missing_fields: "fields differ from the approved demo schema",
  unconstrained_or_invalid_funder_value: "free text or an invalid field value was produced",
  named_reviewer_required: "a named human reviewer is required",
  release_requires_separate_human_review: "release requires a separate human review",
  invalid_rules_file: "the reporting rules could not be validated",
  api_key_not_configured: "configure OPENAI_API_KEY in the local .env file",
  generation_failed: "generation was unavailable; the saved note is unchanged",
};

function renderGeneration(result) {
  const list = document.querySelector("#rule-checks");
  const output = document.querySelector("#generated-views");
  list.replaceChildren();
  output.hidden = true;
  if (!result) {
    generationStatus.textContent = "";
    return;
  }
  generationStatus.classList.toggle("error", ["BLOCKED", "ERROR"].includes(result.status));
  if (result.status === "STALE") {
    generationStatus.textContent = "The note or rules changed. Generate fresh views for review.";
    return;
  }
  if (result.reviewer_name) reviewerInput.value = result.reviewer_name;
  const failures = result.failed_rules || [];
  if (result.status === "BLOCKED") {
    generationStatus.textContent = `BLOCKED — ${failures.join(", ")}. Decision recorded in the ledger.`;
  } else if (result.status === "ERROR") {
    generationStatus.textContent = `ERROR — ${reasonText[result.reason_codes.generation] || "Generation is unavailable."}. Decision recorded in the ledger.`;
  } else {
    generationStatus.textContent = "READY FOR REVIEW — views held locally. Decision recorded in the ledger.";
  }
  for (const [name, check] of Object.entries(result.checks)) {
    const item = document.createElement("li");
    const reason = reasonText[result.reason_codes[name]];
    item.textContent = `${name}: ${check}${reason ? ` — ${reason}` : ""}${name === "release_gate" && check === "PASS" ? " — held locally for review" : ""}`;
    item.classList.toggle("fail", check === "FAIL");
    list.append(item);
  }
  if (result.status === "READY_FOR_REVIEW") {
    document.querySelector("#internal-note").textContent = result.internal_note;
    document.querySelector("#funder-view").textContent = JSON.stringify(result.funder_view, null, 2);
    output.hidden = false;
  }
}

async function loadGeneration() {
  viewsPanel.hidden = false;
  generateButton.disabled = false;
  try {
    renderGeneration(await responseData(await fetch(`/api/drafts/${encodeURIComponent(draftId)}/generation`, { cache: "no-store" })));
  } catch (_) {
    generationStatus.textContent = "Could not load the last generation. Refresh to try again.";
  }
}

async function loadDraft() {
  if (!draftId) {
    const today = new Date();
    dateInput.value = [today.getFullYear(), String(today.getMonth() + 1).padStart(2, "0"), String(today.getDate()).padStart(2, "0")].join("-");
    return;
  }
  fields.disabled = true;
  showStatus("Loading draft…");
  try {
    const draft = await responseData(await fetch(`/api/drafts/${encodeURIComponent(draftId)}`, { cache: "no-store" }));
    referenceInput.value = draft.client_reference;
    dateInput.value = draft.session_date;
    noteInput.value = draft.note;
    setEditTitle();
    fields.disabled = false;
    showStatus("");
    await loadGeneration();
  } catch (error) {
    showStatus(error.message === "Failed to fetch" ? "Could not load the draft. Refresh to try again." : error.message, true);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!referenceInput.value.trim() || !noteInput.value.trim()) {
    showStatus("Enter a client reference and session note before saving.", true);
    return;
  }
  fields.disabled = true;
  saveButton.disabled = true;
  saveButton.textContent = "Saving…";
  showStatus("");
  try {
    const draft = await responseData(await fetch(draftId ? `/api/drafts/${encodeURIComponent(draftId)}` : "/api/drafts", {
      method: draftId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_reference: referenceInput.value, session_date: dateInput.value, note: noteInput.value }),
    }));
    draftId = draft.id;
    window.history.replaceState(null, "", `/notes/${draftId}`);
    setEditTitle();
    dirty = false;
    showStatus("Draft saved.");
    await loadGeneration();
  } catch (error) {
    showStatus(error.message === "Failed to fetch" ? "Could not reach the app. Your text is still here; try saving again." : error.message, true);
  } finally {
    fields.disabled = false;
    saveButton.disabled = false;
    saveButton.textContent = "Save draft";
  }
});

fields.addEventListener("input", () => {
  dirty = true;
  generateButton.disabled = true;
  document.querySelector("#generated-views").hidden = true;
  generationStatus.textContent = "Save your changes before generating views.";
  document.querySelector("#rule-checks").replaceChildren();
});

generateButton.addEventListener("click", async () => {
  if (!draftId || dirty) return;
  generateButton.disabled = true;
  reviewerInput.disabled = true;
  fields.disabled = true;
  generationStatus.classList.remove("error");
  generationStatus.textContent = "Checking rules and generating views…";
  document.querySelector("#generated-views").hidden = true;
  document.querySelector("#rule-checks").replaceChildren();
  try {
    const result = await responseData(await fetch(`/api/drafts/${encodeURIComponent(draftId)}/generate`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewer_name: reviewerInput.value }),
    }));
    renderGeneration(result);
  } catch (error) {
    generationStatus.textContent = error.message === "Failed to fetch" ? "Could not reach the app. Reopen this note to check its latest decision before retrying." : error.message;
    generationStatus.classList.add("error");
  } finally {
    generateButton.disabled = false;
    reviewerInput.disabled = false;
    fields.disabled = false;
  }
});

loadDraft();
