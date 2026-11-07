from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse


app = FastAPI(
    title="Tidepath counseling notes",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
INDEX_PATH = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
