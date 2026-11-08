# Write Once

Repository: `take-one-send-one` (the project's working name; submitted to the DevDay Exchange Codex Hackathon as **Write Once**).

A hackathon prototype for Tidepath Family Alliance to help counselors write session
notes once and prepare funder reports containing only the required information.

The app includes a session-note form, drafts, SQLite persistence, and a single
ledger-first `gate()`. Saved notes can generate a structured internal note and
a constrained funder preview using the OpenAI Agents SDK.
The review panel supports named-human Release and Refuse decisions. The Ledger
page shows the full audit history and calculates released-only quarterly totals.

## Run on Windows

Open PowerShell in this directory. For a fresh setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Test: open <http://127.0.0.1:8000>, save a fictional note, open **Drafts**, reopen
the note, edit it, save, and refresh to confirm the edit persisted.
Stop the server with Ctrl+C.

## Drafts and ledger

- `/` is the new-note form; `/drafts` lists saved drafts; `/notes/{id}` reopens one.
- `/ledger` lists every verified ledger row with its full hash and previous hash.
- The form saves a client reference, session date, and session note.
- Local data is in ignored `data/drafts.db` and `data/ledger.jsonl`.
- Both create and update requests use the one `gate()` in `app/store.py`.
- The gate obtains SQLite's writer lock, verifies the full chain and existing
  drafts' ledger references, appends a ledger row, flushes it, and calls `fsync`.
  Only then does it insert or update the draft and commit SQLite. The lock
  serializes saves across threads and processes; taking it changes no draft data.
- Ledger rows are save **requests**, not proof of a completed save. If SQLite
  fails after the append, the request row remains and the draft is not changed.
  Ledger write or verification failures block draft changes. A partial ledger
  tail also blocks further saves; the app never silently repairs or truncates it.
- Each row contains a version, sequence, event UUID, UTC timestamp, action,
  draft UUID, payload hash, previous hash, and its own SHA-256 hash. The payload
  hash covers the validated JSON save body. Clinical text and client references
  are not copied into the ledger.
- Hash input is UTF-8 JSON with sorted keys and compact separators, excluding
  the row's `hash` field. The first `previous_hash` is 64 zeroes. App writes use
  append mode only. Changed, reordered, and incomplete rows fail verification;
  missing ledger rows referenced by saved drafts also block saves.
- Existing version 1 save rows are preserved. Version 2 generation rows add a
  status, all five checks, failed rule names, reason codes, policy/output hashes,
  generation UUID, and the reviewing counselor's name. The ledger contains no
  source note, generated internal narrative, client reference, or API key.
- Version 3 review rows preserve the same chain and add the review reason and a
  hash of the released funder snapshot. Refusal reasons are reviewer-entered
  audit text; they are displayed on the internal Ledger page.
- Version 4 and later review rows also contain `released_funder_view` on `RELEASED`
  decisions. This is the exact structured funder object, included in the row's
  hash and checked against `released_funder_hash`. Other review decisions store
  `null`. Earlier rows remain unchanged and verifiable.
- New version 5 review rows add a fixed refusal `reason_code`. Only `other` may
  contain optional text in `reason`. The same `no_identifiers` check screens that
  text before any ledger append. A failed check records `BLOCKED` with the unsafe
  text omitted from the ledger, response, and review table.

## Generate views from a saved note

Save or reopen a note, enter the name of the reviewing counselor, and click
**Generate views**. Changes in the form must be saved before generation.
The server uses the saved note, not client-submitted generated content.

`rules/funder_view.json` is an explicitly approved **demo** schema, not a real
grant reporting specification. It is the source of the funder field allowlist
and strict structured-output model. Only reporting period, service type, session
count, language, and service area are allowed; values are constrained to a month,
bounded integers, or enumerations. Unknown language or area stays `unknown`.
Plain-language labels, month names, and language names are a browser display
layer only; the stored and released funder values retain their original schema
keys and codes.

All five decisions are made inside `gate()`:

| Rule | Check |
| --- | --- |
| `allowed_fields` | Require the exact funder schema fields and the expected two-output structure. Invalid rules fail closed. |
| `no_identifiers` | Block client-list requests by diagnosis before any model call. Reject identifier fields, the source client reference, emails, phone-like strings, and diagnosis-code labels in generated funder content. |
| `no_narrative` | Validate funder values against the constrained schema; reject free text, objects, lists, and invalid values. Internal narrative is kept separate. |
| `named_human` | Require a non-placeholder counselor name before normal generation. It is recorded locally and in audit metadata; this prototype does not authenticate that identity. |
| `release_gate` | Hold generation for review. Release requires the explicit review action, current source/rules, valid output hashes, and all five passing checks. Requests to release as part of generation remain blocked. |

A dangerous source request produces a durable `BLOCKED` decision immediately.
Otherwise a `STARTED` ledger row is flushed and synced before the SDK receives
the note. The model has no tools, does not receive the client-reference field,
and is instructed to treat note contents as data. SDK tracing is disabled and
Responses storage is disabled. Generated output is checked again in the gate.
Only a valid result is persisted and shown as `READY_FOR_REVIEW`. A blocked
output is withheld; its raw contents are not put in the ledger. Errors receive
`ERROR` with a static reason code, not raw provider messages. A ledger failure
prevents the model call or storing a result.

The gate holds SQLite's writer lock through the bounded model request, so this
local prototype serializes writes during generation. Editing a note or its rules
makes older generated views `STALE` and hides them until regeneration.

## Release, Refuse, and quarterly totals

On a `READY_FOR_REVIEW` result, enter the reviewing counselor's name and use
**Release** or **Refuse**. Refuse requires a reason from the coded dropdown.
**Other — see internal note for details** allows optional text of up to 1,000
characters, screened for identifiers before logging. Both operations pass through the same `gate()` and append and sync
their ledger row before storing the review decision in SQLite.

- Release records `RELEASED` and approves an immutable local funder snapshot for
  reporting. The gate rechecks the schema, identifier/narrative rules, named
  reviewer, source/policy hashes, reporting month, and original generated output.
- Refuse records `REFUSED`, the reviewer, and the selected code. Only safe Other
  text is retained. The eleven fixed codes and display labels are defined in
  `app/refusals.py`; arbitrary codes and text on non-Other reasons are blocked.
- Blocked or failed generations cannot be released. Stale outputs must be
  regenerated before release. A generation can have only one final review
  decision, and a saved note can be released only once, preventing duplicate totals.
- Reviewed snapshots keep their decision and original data after later edits.
  The review panel identifies when the current note or rules have changed.

Open **Ledger**, select a year and quarter, and click **Quarterly**. Totals use
the released funder view's `reporting_period` month, and show released row count
and summed `session_count`. Held, blocked, refused, and failed results are
excluded. Only committed release records whose ledger row and snapshot hash
verify are counted. If database storage fails after a ledger decision, that
uncommitted decision does not contribute to totals. Broken chains or changed
release snapshots stop calculation rather than returning partial totals.

Release approves local reporting data; this app does not deliver it to a funder.

## Eight fictional demo notes

From the project directory:

```powershell
.\.venv\Scripts\python.exe seed.py
.\.venv\Scripts\python.exe seed.py --run-demo 3
```

The seed is idempotent: it adds eight `SEED-DEMO-01` through `SEED-DEMO-08`
drafts through `gate()` and preserves existing seed drafts and other notes.
Demo 3 is the donor requesting a client list by diagnosis code, with names and
contact details. Running it prints the actual hash-chained ledger decision:
`BLOCKED`, failed rule `no_identifiers`, reason
`client_list_by_diagnosis_requested`. It is blocked before a model call, so this
demo works without an API key. The other notes cover fictional sessions in
English, Spanish, and Haitian Creole, including missing language/area data.

This is a local prototype for fictional notes: SQLite is unencrypted and the app
has no sign-in. The hash chain detects inconsistencies; it is not a signed or
externally anchored audit log. Someone able to rewrite both data files can
rebuild the chain. Identifier screening is a conservative prototype heuristic,
not a comprehensive de-identification system. Real counseling data requires
appropriate approved infrastructure and review.

## Automated tests

From this directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use temporary data directories, Python's standard library, and Uvicorn.
They cover HTTP create/read/update and generation, persistence, all five rules,
ledger-before-database and ledger-before-model ordering, failed writes, tampering,
truncation, concurrent saves, seed idempotence, stale views, and the real SDK's
strict output schema/configuration. Model responses in automated tests are
controlled fixtures; they do not consume API credits.
Review tests also cover named reviewers, refusal reasons, repeated/concurrent
decisions, stale or altered content, append-before-store ordering, failed
storage, and released-only quarterly boundaries and snapshots.

## API key

Set `OPENAI_API_KEY` directly in the ignored project `.env` file. Never paste it
into chat, commit it, or print it. The server reads it only when a safe generation
passes its preflight checks. Optional `OPENAI_MODEL` defaults to `gpt-6-astra`.
Only `.env.example`, with an empty key, belongs in version control. No key means
normal generation records `ERROR: api_key_not_configured`; it never substitutes
a fake model response. The blocked demo still works without a key.

## Dependencies

FastAPI, the approved Uvicorn server, and the OpenAI Agents SDK, plus their required dependencies.
SQLite and the test tools come from Python's standard library.

The SDK integration follows the official [OpenAI Agent definitions guide](https://developers.openai.com/api/docs/guides/agents/define-agents).

## Credit

Built by Michelle Poe, Integrity AI 2.0. The hold, release and ledger pattern
shown here is the public shape of RHITL™ (patent pending); the production rule
engine and ledger design are not part of this repository.
