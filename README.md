# Take One, Send One

A hackathon prototype for Tidepath Family Alliance to help counselors write session
notes once and prepare funder reports containing only the required information.

The app includes a session-note form, drafts, SQLite persistence, and a single
ledger-first `gate()`. Saved notes can generate a structured internal note and
a constrained funder preview using the OpenAI Agents SDK.

## Run on Windows

Open PowerShell in this directory. For a fresh setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The development environment prepared in this chat can also run it from this directory:

```powershell
..\..\work\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Test: open <http://127.0.0.1:8000>, save a fictional note, open **Drafts**, reopen
the note, edit it, save, and refresh to confirm the edit persisted.
Stop the server with Ctrl+C.

## Drafts and ledger

- `/` is the new-note form; `/drafts` lists saved drafts; `/notes/{id}` reopens one.
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

## Generate views from a saved note

Save or reopen a note, enter the name of the reviewing counselor, and click
**Generate views**. Changes in the form must be saved before generation.
The server uses the saved note, not client-submitted generated content.

`rules/funder_view.json` is an explicitly approved **demo** schema, not a real
grant reporting specification. It is the source of the funder field allowlist
and strict structured-output model. Only reporting period, service type, session
count, language, and service area are allowed; values are constrained to a month,
bounded integers, or enumerations. Unknown language or area stays `unknown`.

All five decisions are made inside `gate()`:

| Rule | Check |
| --- | --- |
| `allowed_fields` | Require the exact funder schema fields and the expected two-output structure. Invalid rules fail closed. |
| `no_identifiers` | Block client-list requests by diagnosis before any model call. Reject identifier fields, the source client reference, emails, phone-like strings, and diagnosis-code labels in generated funder content. |
| `no_narrative` | Validate funder values against the constrained schema; reject free text, objects, lists, and invalid values. Internal narrative is kept separate. |
| `named_human` | Require a non-placeholder counselor name before normal generation. It is recorded locally and in audit metadata; this prototype does not authenticate that identity. |
| `release_gate` | Hold generated views locally for human review. Every release request is blocked; there is no sending, exporting, or release capability in this step. A name is not release approval. |

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

## Eight fictional demo notes

From the project directory with the environment prepared in this chat:

```powershell
..\..\work\.venv\Scripts\python.exe seed.py
..\..\work\.venv\Scripts\python.exe seed.py --run-demo 3
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

From this directory, using the environment prepared in this chat:

```powershell
..\..\work\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For a fresh setup, replace that Python path with `.\.venv\Scripts\python.exe`.
Tests use temporary data directories, Python's standard library, and Uvicorn.
They cover HTTP create/read/update and generation, persistence, all five rules,
ledger-before-database and ledger-before-model ordering, failed writes, tampering,
truncation, concurrent saves, seed idempotence, stale views, and the real SDK's
strict output schema/configuration. Model responses in automated tests are
controlled fixtures; they do not consume API credits.

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
