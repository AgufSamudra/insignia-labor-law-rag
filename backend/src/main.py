import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

try:
    from .upload_file import make_document_id, process_pdf_document
except ImportError:  # Supports running this file directly with `python src/main.py`.
    from upload_file import make_document_id, process_pdf_document


app = FastAPI(
    title="Insignia Labor Law Assistant API",
    description="API backend for the Insignia labor law assistant.",
    version="0.1.0",
)

CHUNKS_DIR = Path(__file__).resolve().parents[1] / "data" / "chunks"

# Allow the Vite development server to call the API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["system"])
async def root() -> dict[str, str]:
    """Return basic information about the API."""
    return {"message": "Insignia Labor Law Assistant API is running"}


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Simple health check for local development and deployments."""
    return {"status": "ok"}


# NOTE: endpoint only still into jsonl file, not into vector database yet. 

@app.post("/upload_document", tags=["documents"])
@app.post("/upload-documents", tags=["documents"])
@app.post("/documents/upload", tags=["documents"])
async def upload_documents(
    files: Annotated[
        list[UploadFile],
        File(description="One or more PDF documents to process"),
    ],
) -> dict[str, object]:
    """Process multiple uploaded PDF documents and save their JSONL chunks."""
    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    processed_files: list[dict[str, object]] = []
    failed_files: list[dict[str, str]] = []

    with TemporaryDirectory(prefix="insignia-upload-") as temporary_directory:
        temporary_root = Path(temporary_directory)

        for uploaded_file in files:
            original_filename = Path(uploaded_file.filename or "document.pdf").name
            if Path(original_filename).suffix.lower() != ".pdf":
                failed_files.append(
                    {
                        "filename": original_filename,
                        "error": "Hanya file PDF yang dapat diproses",
                    }
                )
                await uploaded_file.close()
                continue

            file_workspace = temporary_root / uuid4().hex
            file_workspace.mkdir()
            pdf_path = file_workspace / original_filename
            output_filename = (
                f"{make_document_id(original_filename)}-{uuid4().hex[:8]}.jsonl"
            )
            output_path = CHUNKS_DIR / output_filename

            try:
                await uploaded_file.seek(0)
                with pdf_path.open("wb") as destination:
                    shutil.copyfileobj(uploaded_file.file, destination)

                chunks = await run_in_threadpool(
                    process_pdf_document,
                    pdf_path,
                    output_path,
                )
                processed_files.append(
                    {
                        "filename": original_filename,
                        "chunks": len(chunks),
                        "output_file": output_filename,
                    }
                )
            except Exception as error:
                failed_files.append(
                    {
                        "filename": original_filename,
                        "error": str(error),
                    }
                )
            finally:
                await uploaded_file.close()

    return {
        "message": (
            f"{len(processed_files)} document(s) processed, "
            f"{len(failed_files)} failed"
        ),
        "processed": processed_files,
        "failed": failed_files,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
