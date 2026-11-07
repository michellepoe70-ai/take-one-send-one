"use strict";

async function loadDrafts() {
  const status = document.querySelector("#status");
  try {
    const response = await fetch("/api/drafts", { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load drafts. Refresh to try again.");
    const drafts = await response.json();
    document.querySelector("#empty-state").hidden = drafts.length !== 0;
    const list = document.querySelector("#draft-list");
    for (const draft of drafts) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.className = "draft-link";
      link.href = `/notes/${encodeURIComponent(draft.id)}`;
      const details = document.createElement("div");
      const reference = document.createElement("strong");
      reference.textContent = draft.client_reference;
      const session = document.createElement("p");
      session.textContent = `Session: ${draft.session_date}`;
      const updated = document.createElement("p");
      updated.textContent = `Saved ${new Date(draft.updated_at).toLocaleString()}`;
      const action = document.createElement("span");
      action.textContent = "Open draft →";
      details.append(reference, session, updated);
      link.append(details, action);
      item.append(link);
      list.append(item);
    }
    status.textContent = "";
  } catch (error) {
    status.textContent = error.message === "Failed to fetch" ? "Could not reach the app. Refresh to try again." : error.message;
    status.classList.add("error");
  }
}

loadDrafts();
