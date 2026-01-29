import os
import uuid
import asyncio
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.models.schemas import Language, TranslationStatus, LANGUAGE_NAMES
from app.services.ppt_service import PPTService
from app.agents.orchestrator import TranslationOrchestrator

app = FastAPI(
    title="PPT Translator",
    description="Translate PowerPoint presentations with AI-powered multi-agent review",
    version="1.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directories
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"

UPLOAD_DIR.mkdir(exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# In-memory status tracking
translation_status: dict[str, TranslationStatus] = {}


def get_api_key() -> str:
    """Get API key from environment."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY environment variable not set",
        )
    return api_key


@app.get("/")
async def root():
    """Serve the main page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/languages")
async def get_languages():
    """Get available languages."""
    return {
        "languages": [
            {"code": lang.value, "name": LANGUAGE_NAMES[lang]} for lang in Language
        ]
    }


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a PowerPoint file."""
    if not file.filename.endswith((".pptx", ".ppt")):
        raise HTTPException(status_code=400, detail="Only .pptx and .ppt files are supported")

    # Generate unique ID
    file_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / f"{file_id}.pptx"

    # Save file
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    # Get file info
    try:
        ppt_service = PPTService(str(file_path))
        slide_count = ppt_service.get_slide_count()
        texts = ppt_service.get_all_texts()
    except Exception as e:
        os.remove(file_path)
        raise HTTPException(status_code=400, detail=f"Error processing file: {str(e)}")

    return {
        "file_id": file_id,
        "filename": file.filename,
        "slide_count": slide_count,
        "text_count": len(texts),
    }


async def process_translation(
    file_id: str,
    source_lang: Language,
    target_lang: Language,
    min_review_loops: int,
):
    """Background task for translation processing."""
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            translation_status[file_id] = TranslationStatus(
                status="error",
                progress=0,
                total_slides=0,
                current_slide=0,
                review_loop=0,
                message="API key not configured",
            )
            return

        file_path = UPLOAD_DIR / f"{file_id}.pptx"
        output_path = UPLOAD_DIR / f"{file_id}_translated.pptx"

        ppt_service = PPTService(str(file_path))
        orchestrator = TranslationOrchestrator(api_key, min_review_loops)

        # Extract texts
        texts_data = ppt_service.get_all_texts()
        unique_texts = list(set(item["text"] for item in texts_data))
        total_texts = len(unique_texts)

        translation_status[file_id] = TranslationStatus(
            status="processing",
            progress=0,
            total_slides=ppt_service.get_slide_count(),
            current_slide=0,
            review_loop=0,
            message=f"Starting translation of {total_texts} text elements...",
        )

        # Translate each unique text
        translations = {}
        for idx, text in enumerate(unique_texts):
            # Update status
            translation_status[file_id] = TranslationStatus(
                status="processing",
                progress=int((idx / total_texts) * 100),
                total_slides=ppt_service.get_slide_count(),
                current_slide=idx + 1,
                review_loop=0,
                message=f"Translating text {idx + 1}/{total_texts}...",
            )

            def update_review_status(iteration: int, message: str):
                translation_status[file_id] = TranslationStatus(
                    status="processing",
                    progress=int((idx / total_texts) * 100),
                    total_slides=ppt_service.get_slide_count(),
                    current_slide=idx + 1,
                    review_loop=iteration,
                    message=f"Text {idx + 1}/{total_texts}: {message}",
                )

            result = await orchestrator.translate_with_review(
                text=text,
                source_lang=source_lang,
                target_lang=target_lang,
                progress_callback=update_review_status,
            )
            translations[text] = result.translated

        # Apply translations
        translation_status[file_id] = TranslationStatus(
            status="processing",
            progress=95,
            total_slides=ppt_service.get_slide_count(),
            current_slide=total_texts,
            review_loop=min_review_loops,
            message="Applying translations to PowerPoint...",
        )

        ppt_service.apply_translations(translations, str(output_path))

        translation_status[file_id] = TranslationStatus(
            status="completed",
            progress=100,
            total_slides=ppt_service.get_slide_count(),
            current_slide=total_texts,
            review_loop=min_review_loops,
            message="Translation completed successfully!",
        )

    except Exception as e:
        translation_status[file_id] = TranslationStatus(
            status="error",
            progress=0,
            total_slides=0,
            current_slide=0,
            review_loop=0,
            message=f"Error: {str(e)}",
        )


@app.post("/api/translate/{file_id}")
async def start_translation(
    file_id: str,
    background_tasks: BackgroundTasks,
    source_language: str = Form(...),
    target_language: str = Form(...),
    min_review_loops: int = Form(5),
):
    """Start translation process."""
    file_path = UPLOAD_DIR / f"{file_id}.pptx"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        source_lang = Language(source_language)
        target_lang = Language(target_language)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid language code")

    if source_lang == target_lang:
        raise HTTPException(status_code=400, detail="Source and target languages must be different")

    # Initialize status
    translation_status[file_id] = TranslationStatus(
        status="queued",
        progress=0,
        total_slides=0,
        current_slide=0,
        review_loop=0,
        message="Translation queued...",
    )

    # Start background task
    background_tasks.add_task(
        process_translation,
        file_id,
        source_lang,
        target_lang,
        min_review_loops,
    )

    return {"message": "Translation started", "file_id": file_id}


@app.get("/api/status/{file_id}")
async def get_status(file_id: str):
    """Get translation status."""
    if file_id not in translation_status:
        raise HTTPException(status_code=404, detail="Translation not found")

    return translation_status[file_id]


@app.get("/api/download/{file_id}")
async def download_file(file_id: str):
    """Download translated file."""
    output_path = UPLOAD_DIR / f"{file_id}_translated.pptx"

    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Translated file not found")

    return FileResponse(
        path=output_path,
        filename="translated.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


@app.delete("/api/file/{file_id}")
async def delete_file(file_id: str):
    """Delete uploaded and translated files."""
    file_path = UPLOAD_DIR / f"{file_id}.pptx"
    output_path = UPLOAD_DIR / f"{file_id}_translated.pptx"

    if file_path.exists():
        os.remove(file_path)
    if output_path.exists():
        os.remove(output_path)

    if file_id in translation_status:
        del translation_status[file_id]

    return {"message": "Files deleted"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
