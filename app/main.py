import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.store import DraftInput, DraftNotFoundError, GenerationNotFoundError, LedgerIntegrityError, Store, gate


STATIC_DIR = Path(__file__).parent / "static"


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reviewer_name: str = Field(default="", max_length=100)
    release_requested: bool = False


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reviewer_name: str = Field(default="", max_length=100)
    reason_code: str = Field(default="", max_length=80)
    reason: str = Field(default="", max_length=1000)


def create_app(data_dir: Path | None = None) -> FastAPI:
    store = Store(data_dir if data_dir is not None else Path(__file__).parent.parent / "data")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        store.initialize()
        yield

    application = FastAPI(
        title="Take One, Send One",
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
    )

    @application.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/", include_in_schema=False)
    def note_form() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/notes/{draft_id}", include_in_schema=False)
    def edit_form(draft_id: UUID) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/drafts", include_in_schema=False)
    def drafts_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "drafts.html")

    @application.get("/ledger", include_in_schema=False)
    def ledger_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "ledger.html")

    @application.get("/api/ledger")
    def ledger_rows() -> dict:
        try:
            return store.ledger_rows()
        except (LedgerIntegrityError, OSError, sqlite3.Error):
            raise HTTPException(503, "Ledger verification failed. Rows were not displayed.") from None

    @application.get("/api/quarterly")
    def quarterly_totals(year: int = Query(ge=1, le=9999), quarter: int = Query(ge=1, le=4)) -> dict:
        try:
            return store.quarterly_totals(year, quarter)
        except (LedgerIntegrityError, OSError, sqlite3.Error):
            raise HTTPException(503, "Ledger or released snapshot verification failed. Totals were not calculated.") from None

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/drafts")
    def list_drafts() -> list[dict]:
        return store.list_drafts()

    @application.get("/api/drafts/{draft_id}")
    def get_draft(draft_id: UUID) -> dict:
        try:
            return store.get_draft(str(draft_id))
        except DraftNotFoundError:
            raise HTTPException(404, "Draft not found.") from None

    def save(draft: DraftInput, draft_id: str | None = None) -> dict:
        try:
            return gate(store, draft, draft_id)
        except DraftNotFoundError:
            raise HTTPException(404, "Draft not found.") from None
        except LedgerIntegrityError:
            raise HTTPException(503, "Ledger verification failed. Draft was not saved.") from None
        except OSError:
            raise HTTPException(503, "Ledger could not be written. Draft was not saved.") from None
        except sqlite3.Error:
            raise HTTPException(503, "Draft could not be saved. Keep your text and try again.") from None

    @application.post("/api/drafts", status_code=201)
    def create_draft(draft: DraftInput) -> dict:
        return save(draft)

    @application.put("/api/drafts/{draft_id}")
    def update_draft(draft_id: UUID, draft: DraftInput) -> dict:
        return save(draft, str(draft_id))

    @application.get("/api/drafts/{draft_id}/generation")
    def latest_generation(draft_id: UUID) -> dict | None:
        try:
            return store.latest_generation(str(draft_id))
        except DraftNotFoundError:
            raise HTTPException(404, "Draft not found.") from None

    @application.post("/api/drafts/{draft_id}/generate")
    def generate(draft_id: UUID, request: GenerateRequest) -> dict:
        try:
            return gate(
                store, draft_id=str(draft_id), action="generate",
                reviewer_name=request.reviewer_name, release_requested=request.release_requested,
            )
        except DraftNotFoundError:
            raise HTTPException(404, "Draft not found.") from None
        except LedgerIntegrityError:
            raise HTTPException(503, "Ledger verification failed. Generation was stopped.") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Generation could not be recorded. Keep the saved note and try again.") from None

    def review(generation_id: UUID, request: ReviewRequest, action: str) -> dict:
        try:
            return gate(
                store, action=action, generation_id=str(generation_id),
                reviewer_name=request.reviewer_name, reason_code=request.reason_code, reason=request.reason,
            )
        except (GenerationNotFoundError, DraftNotFoundError):
            raise HTTPException(404, "Generated views not found.") from None
        except LedgerIntegrityError:
            raise HTTPException(503, "Ledger verification failed. Review was stopped.") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Review could not be recorded. Reopen the note to check its status before retrying.") from None

    @application.post("/api/generations/{generation_id}/release")
    def release(generation_id: UUID, request: ReviewRequest) -> dict:
        return review(generation_id, request, "release")

    @application.post("/api/generations/{generation_id}/refuse")
    def refuse(generation_id: UUID, request: ReviewRequest) -> dict:
        return review(generation_id, request, "refuse")

    return application


app = create_app()
