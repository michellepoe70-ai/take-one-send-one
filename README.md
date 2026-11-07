# Take One, Send One

A hackathon prototype for Tidepath Family Alliance to help counselors write session
notes once and prepare funder reports containing only the required information.

Step 1 is a local FastAPI scaffold: a placeholder page and a health endpoint.
No notes, funder reports, database records, or AI calls are implemented yet.

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

Test: open <http://127.0.0.1:8000>, click **Check app status**, and expect `{"status":"ok"}`.
Stop the server with Ctrl+C.

## API key

The ignored `.env` file contains an empty `OPENAI_API_KEY` field. Add a key there
when AI functionality is implemented. The scaffold does not read or use it.
Only `.env.example`, with an empty value, belongs in version control.

## Dependencies

FastAPI and the approved Uvicorn server, plus their required dependencies.
SQLite is available in Python's standard library and will be used when persistence
is implemented. The OpenAI Agents SDK will be added when AI functionality is built.
