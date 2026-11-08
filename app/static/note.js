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
const reviewerBanner = document.querySelector("#reviewer-banner");
const generationStatus = document.querySelector("#generation-status");
const releaseButton = document.querySelector("#release-button");
const refuseButton = document.querySelector("#refuse-button");
const refusalReason = document.querySelector("#refusal-reason");
const refusalOther = document.querySelector("#refusal-other");
const refusalOtherText = document.querySelector("#refusal-other-text");
const reviewStatus = document.querySelector("#review-status");
let currentGeneration = null;
let dirty = false;
let draftId = window.location.pathname.startsWith("/notes/")
  ? window.location.pathname.split("/")[2] : null;
let draftOpened = !draftId;
let openingDraft = false;
const reviewerSessionKey = "take-one-send-one.reviewer_name";
try {
  reviewerInput.value = sessionStorage.getItem(reviewerSessionKey) || reviewerInput.value;
} catch (_) { /* The existing name field still works when storage is unavailable. */ }

function rememberReviewer() {
  try {
    sessionStorage.setItem(reviewerSessionKey, reviewerInput.value.trim());
  } catch (_) { /* Session storage is optional. */ }
}

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
  api_connection_failed: "the app could not connect to OpenAI; the saved note is unchanged",
  api_request_timeout: "the OpenAI request timed out; the saved note is unchanged",
  api_authentication_failed: "OpenAI rejected the API key; check the local .env file",
  api_permission_denied: "the API key does not have permission for this request",
  api_quota_exhausted: "the OpenAI project has no available API credit or quota",
  api_rate_limited: "OpenAI rate limited this request; wait before trying again",
  api_model_unavailable: "the configured OpenAI model is unavailable to this API key",
  api_request_rejected: "OpenAI rejected the generation request",
  api_service_error: "OpenAI returned a service error; try again later",
  invalid_model_output: "the generated output did not match the required structure",
  generation_event_loop_closed: "the generation runtime closed unexpectedly; restart the app",
  generation_runtime_error: "the generation runtime failed; the saved note is unchanged",
  generation_not_ready: "only held, valid views can be reviewed",
  already_reviewed: "this output already has a final review decision",
  newer_generation_exists: "reopen the latest generated views before reviewing",
  refusal_reason_required: "select a refusal reason",
  refusal_reason_invalid: "select one of the listed refusal reasons",
  refusal_details_only_for_other: "use the Other option for additional text",
  identifier_in_refusal_reason: "identifying detail found in Other text; remove it before refusing",
  refusal_reason_too_long: "keep the reason within 1,000 characters",
  note_already_released: "this note has already been released and counted",
  rules_changed: "the reporting rules changed; generate fresh views",
  stale_generation: "the saved note changed; generate fresh views",
  generated_content_changed: "the generated content does not match its ledger record",
  reporting_period_mismatch: "the reporting month does not match the saved session date",
};

function renderFunderView(view) {
  const summary = document.querySelector("#funder-view");
  summary.replaceChildren();
  const period = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(view.reporting_period || "");
  const reportingPeriod = period
    ? new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" })
        .format(new Date(Date.UTC(Number(period[1]), Number(period[2]) - 1, 1)))
    : "Not recorded";
  const languages = { en: "English", es: "Spanish", ht: "Haitian Creole", unknown: "Not recorded" };
  const entries = [
    ["Reporting period", reportingPeriod],
    ["Service", view.service_type === "counseling" ? "Counseling" : "Not recorded"],
    ["Sessions", view.session_count],
    ["Language", languages[view.language] || "Not recorded"],
    ["Service area", view.service_area === "unknown" ? "Not recorded" : view.service_area],
  ];
  for (const [label, value] of entries) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value;
    row.append(term, description);
    summary.append(row);
  }
}

function renderGeneration(result) {
  generationStatus.dataset.state = result?.status || "";
  currentGeneration = result;
  const list = document.querySelector("#rule-checks");
  const output = document.querySelector("#generated-views");
  list.replaceChildren();
  output.hidden = true;
  document.querySelector("#review-controls").hidden = true;
  document.querySelector("#review-receipt").textContent = "";
  reviewStatus.textContent = "";
  if (!result) {
    generationStatus.textContent = "";
    return;
  }
  generationStatus.classList.toggle("error", ["BLOCKED", "ERROR"].includes(result.status));
  if (result.status === "STALE") {
    generationStatus.textContent = "The note or rules changed. Generate fresh views for review.";
    return;
  }
  if (!reviewerInput.value.trim() && result.reviewer_name) reviewerInput.value = result.reviewer_name;
  const failures = result.failed_rules || [];
  if (result.status === "BLOCKED") {
    generationStatus.textContent = `BLOCKED — ${failures.join(", ")}. Decision recorded in the ledger.`;
  } else if (result.status === "ERROR") {
    generationStatus.textContent = `ERROR — ${reasonText[result.reason_codes.generation] || "Generation is unavailable."}. Decision recorded in the ledger.`;
  } else if (result.status === "RELEASED") {
    generationStatus.textContent = "RELEASED — approved for reporting and included in quarterly totals.";
  } else if (result.status === "REFUSED") {
    generationStatus.textContent = "REFUSED — review decision recorded in the ledger.";
  } else {
    generationStatus.textContent = "READY FOR REVIEW — views held locally. Decision recorded in the ledger.";
  }
  for (const [name, check] of Object.entries(result.checks)) {
    const item = document.createElement("li");
    item.dataset.state = check;
    const reason = reasonText[result.reason_codes[name]];
    item.textContent = `${name}: ${check}${reason ? ` — ${reason}` : ""}${name === "release_gate" && check === "PASS" && result.status === "READY_FOR_REVIEW" ? " — held locally for review" : ""}`;
    item.classList.toggle("fail", check === "FAIL");
    list.append(item);
  }
  if (["READY_FOR_REVIEW", "RELEASED", "REFUSED"].includes(result.status)) {
    document.querySelector("#internal-note").textContent = result.internal_note;
    renderFunderView(result.funder_view);
    output.hidden = false;
    document.querySelector("#review-controls").hidden = result.status !== "READY_FOR_REVIEW";
    if (result.reviewed_at) {
      const refusalLabel = [...refusalReason.options].find(option => option.value && option.value === result.review_reason_code)?.textContent;
      const reviewReason = [refusalLabel, result.review_reason].filter(Boolean).join(": ");
      document.querySelector("#review-receipt").textContent = `Reviewed by ${result.reviewer_name} on ${new Date(result.reviewed_at).toLocaleString()}${reviewReason ? ` — ${reviewReason}` : ""}${result.is_current === false ? ". This is the reviewed snapshot; the note or rules have since changed." : ""}`;
    }
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
  if (draftOpened || openingDraft) return;
  fields.disabled = true;
  viewsPanel.hidden = false;
  generateButton.disabled = true;
  if (!reviewerInput.value.trim()) {
    reviewerBanner.hidden = false;
    showStatus("");
    reviewerInput.focus();
    return;
  }
  openingDraft = true;
  reviewerInput.disabled = true;
  showStatus("Loading draft…");
  try {
    const reviewer = encodeURIComponent(reviewerInput.value.trim());
    const draft = await responseData(await fetch(`/api/drafts/${encodeURIComponent(draftId)}?reviewer_name=${reviewer}`, { cache: "no-store" }));
    referenceInput.value = draft.client_reference;
    dateInput.value = draft.session_date;
    noteInput.value = draft.note;
    reviewerBanner.hidden = true;
    draftOpened = true;
    rememberReviewer();
    fields.disabled = false;
    showStatus("");
    await loadGeneration();
  } catch (error) {
    showStatus(error.message === "Failed to fetch" ? "Could not load the draft. Refresh to try again." : error.message, true);
  } finally {
    openingDraft = false;
    reviewerInput.disabled = false;
  }
}

reviewerInput.addEventListener("blur", () => {
  rememberReviewer();
  if (draftId && !draftOpened) loadDraft();
});

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
  reviewStatus.textContent = "";
});

async function reviewViews(action) {
  if (dirty || currentGeneration?.status !== "READY_FOR_REVIEW") return;
  reviewStatus.classList.add("error");
  reviewStatus.dataset.state = "ERROR";
  if (!reviewerInput.value.trim()) {
    reviewStatus.textContent = "Enter the reviewing counselor's name before releasing or refusing.";
    return;
  }
  if (action === "refuse" && !refusalReason.value) {
    reviewStatus.textContent = "Select a refusal reason.";
    return;
  }
  const identifier = currentGeneration.id;
  releaseButton.disabled = refuseButton.disabled = generateButton.disabled = true;
  reviewerInput.disabled = refusalReason.disabled = refusalOtherText.disabled = fields.disabled = true;
  reviewStatus.classList.remove("error");
  reviewStatus.dataset.state = "STARTED";
  reviewStatus.textContent = "Recording review…";
  try {
    const result = await responseData(await fetch(`/api/generations/${encodeURIComponent(identifier)}/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reviewer_name: reviewerInput.value,
        reason_code: action === "refuse" ? refusalReason.value : "",
        reason: action === "refuse" && refusalReason.value === "other" ? refusalOtherText.value : "",
      }),
    }));
    if (result.status === "BLOCKED") {
      reviewStatus.dataset.state = "BLOCKED";
      reviewStatus.textContent = `BLOCKED — ${result.failed_rules.map(name => `${name}: ${reasonText[result.reason_codes[name]] || result.reason_codes[name]}`).join("; ")}. Recorded in the ledger.`;
      reviewStatus.classList.add("error");
    } else {
      refusalReason.value = "";
      refusalOtherText.value = "";
      refusalOther.hidden = true;
      await loadGeneration();
    }
  } catch (error) {
    reviewStatus.textContent = error.message === "Failed to fetch" ? "Could not reach the app. Reopen this note to check its review status before retrying." : error.message;
    reviewStatus.dataset.state = "ERROR";
    reviewStatus.classList.add("error");
  } finally {
    releaseButton.disabled = refuseButton.disabled = generateButton.disabled = false;
    reviewerInput.disabled = refusalReason.disabled = refusalOtherText.disabled = fields.disabled = false;
  }
}

releaseButton.addEventListener("click", () => reviewViews("release"));
refuseButton.addEventListener("click", () => reviewViews("refuse"));
refusalReason.addEventListener("change", () => {
  refusalOther.hidden = refusalReason.value !== "other";
});

generateButton.addEventListener("click", async () => {
  if (!draftId || dirty) return;
  generateButton.disabled = true;
  reviewerInput.disabled = true;
  fields.disabled = true;
  generationStatus.classList.remove("error");
  generationStatus.dataset.state = "STARTED";
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
    generationStatus.dataset.state = "ERROR";
    generationStatus.classList.add("error");
  } finally {
    generateButton.disabled = false;
    reviewerInput.disabled = false;
    fields.disabled = false;
  }
});

loadDraft();
