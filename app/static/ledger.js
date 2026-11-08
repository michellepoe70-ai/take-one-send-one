"use strict";

const yearInput = document.querySelector("#report-year");
const quarterInput = document.querySelector("#report-quarter");
const today = new Date();
yearInput.value = today.getFullYear();
quarterInput.value = Math.floor(today.getMonth() / 3) + 1;

async function readResponse(response) {
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Check the reporting period.");
  return body;
}

async function loadLedger() {
  const status = document.querySelector("#ledger-status");
  try {
    const ledger = await readResponse(await fetch("/api/ledger", { cache: "no-store" }));
    status.textContent = `${ledger.count} ledger rows verified.`;
    status.dataset.state = "verified";
    document.querySelector("#head-hash").textContent = ledger.head_hash;
    const list = document.querySelector("#ledger-rows");
    for (const row of [...ledger.rows].reverse()) {
      const item = document.createElement("li");
      item.className = "panel";
      const header = document.createElement("div");
      header.className = "entry-heading";
      const sequence = document.createElement("span");
      sequence.className = "entry-sequence";
      sequence.textContent = `#${row.sequence}`;
      const heading = document.createElement("h3");
      const titles = {
        "draft.create.requested": "New draft requested",
        "draft.update.requested": "Draft update requested",
        "views.generate": "Note views generation",
        "views.release": "Release decision",
        "views.refuse": "Refusal decision",
      };
      heading.textContent = row.event === "VIEWED" ? "Draft opened for review" : (titles[row.action] || row.action);
      const badge = document.createElement("span");
      badge.className = "state-badge";
      if (row.event) {
        badge.dataset.event = row.event;
        badge.textContent = row.event;
      } else {
        badge.dataset.state = row.status || "REQUESTED";
        badge.textContent = (row.status || "REQUESTED").replaceAll("_", " ");
      }
      header.append(sequence, heading, badge);
      const metadata = document.createElement("p");
      metadata.className = "ledger-meta";
      metadata.textContent = `${new Date(row.timestamp).toLocaleString()} · ${row.event || row.action}${row.reviewer_name ? ` · ${row.reviewer_name}` : ""}`;
      const decision = document.createElement("p");
      decision.className = "ledger-decision";
      const refusalLabel = row.reason_code === "other" ? "Other — see internal note for details"
        : row.reason_code?.replaceAll("_", " ");
      const refusal = [refusalLabel, row.reason].filter(Boolean).join(": ");
      decision.textContent = [
        refusal ? `Reason: ${refusal}` : "",
        row.failed_rules?.length ? `Failed rules: ${row.failed_rules.join(", ")}` : "",
      ].filter(Boolean).join(" · ");
      item.append(header, metadata, decision);
      const hashes = document.createElement("div");
      hashes.className = "hash-grid";
      for (const [label, key] of [["Hash", "hash"], ["Previous hash", "previous_hash"]]) {
        const text = document.createElement("p");
        text.className = "hash-item";
        const caption = document.createElement("strong");
        caption.textContent = label;
        const hash = document.createElement("code");
        hash.textContent = row[key];
        text.append(caption, hash);
        hashes.append(text);
      }
      item.append(hashes);
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "All row fields";
      const fullRow = document.createElement("pre");
      fullRow.textContent = JSON.stringify(row, null, 2);
      details.append(summary, fullRow);
      item.append(details);
      list.append(item);
    }
  } catch (error) {
    status.textContent = error.message === "Failed to fetch" ? "Could not reach the app. Refresh to try again." : error.message;
    status.classList.add("error");
    status.dataset.state = "error";
  }
}

document.querySelector("#quarterly-form").addEventListener("submit", async event => {
  event.preventDefault();
  const status = document.querySelector("#quarterly-status");
  const button = document.querySelector("#quarterly-button");
  document.querySelector("#quarterly-totals").hidden = true;
  status.classList.remove("error");
  status.textContent = "Calculating released totals…";
  button.disabled = true;
  try {
    const totals = await readResponse(await fetch(`/api/quarterly?year=${encodeURIComponent(yearInput.value)}&quarter=${encodeURIComponent(quarterInput.value)}`, { cache: "no-store" }));
    document.querySelector("#released-count").textContent = totals.released_rows;
    document.querySelector("#session-count").textContent = totals.session_count;
    document.querySelector("#quarterly-totals").hidden = false;
    status.textContent = `${totals.year} Q${totals.quarter} · released rows only.`;
  } catch (error) {
    status.textContent = error.message === "Failed to fetch" ? "Could not reach the app. Try again." : error.message;
    status.classList.add("error");
  } finally {
    button.disabled = false;
  }
});

loadLedger();
