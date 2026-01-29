import os
import re
import uuid
import aiofiles
import structlog
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.models.schemas import Language, TranslationStatus, LANGUAGE_NAMES
from app.services.ppt_service import PPTService
from app.agents.orchestrator import TranslationOrchestrator

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
UUID_PATTERN = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")

# Rate limiter
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set - translations will fail")
    else:
        logger.info("Application started", api_key_configured=True)

    UPLOAD_DIR.mkdir(exist_ok=True)
    yield

    # Shutdown
    logger.info("Application shutting down")


app = FastAPI(
    title="PPT Translator",
    description="AI 기반 멀티에이전트 PPT 번역 서비스",
    version="2.0.0",
    lifespan=lifespan,
)

# Add rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware - restrict in production
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Directories
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# In-memory status tracking (for single instance)
translation_status: dict[str, TranslationStatus] = {}


def validate_file_id(file_id: str) -> str:
    """Validate file_id is a valid UUID to prevent path traversal."""
    if not UUID_PATTERN.match(file_id):
        raise HTTPException(status_code=400, detail="잘못된 파일 ID 형식입니다")
    return file_id


def get_api_key() -> str:
    """Get API key from environment."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="API 키가 설정되지 않았습니다",
        )
    return api_key


@app.get("/")
async def root():
    """Serve the main page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health_check():
    """Health check endpoint for deployment verification."""
    checks = {
        "status": "healthy",
        "checks": {
            "api_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "upload_dir_exists": UPLOAD_DIR.exists(),
        },
    }

    # Verify upload directory is writable
    try:
        test_file = UPLOAD_DIR / ".health_check"
        test_file.touch()
        test_file.unlink()
        checks["checks"]["upload_dir_writable"] = True
    except Exception:
        checks["checks"]["upload_dir_writable"] = False
        checks["status"] = "unhealthy"

    status_code = 200 if checks["status"] == "healthy" else 503
    return JSONResponse(checks, status_code=status_code)


@app.get("/api/languages")
async def get_languages():
    """Get available languages."""
    return {
        "languages": [
            {"code": lang.value, "name": LANGUAGE_NAMES[lang]} for lang in Language
        ]
    }


@app.post("/api/upload")
@limiter.limit("10/minute")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Upload a PowerPoint file."""
    # Only accept .pptx (python-pptx doesn't support .ppt)
    if not file.filename or not file.filename.lower().endswith(".pptx"):
        raise HTTPException(
            status_code=400,
            detail=".pptx 파일만 지원됩니다 (.ppt 파일은 .pptx로 변환 후 업로드하세요)"
        )

    # Check file size from header
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"파일 크기가 너무 큽니다. 최대 {MAX_FILE_SIZE // 1024 // 1024}MB까지 지원됩니다",
        )

    # Generate unique ID
    file_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / f"{file_id}.pptx"

    logger.info("file_upload_started", file_id=file_id, filename=file.filename)

    # Stream file to disk
    try:
        total_size = 0
        async with aiofiles.open(file_path, "wb") as out_file:
            while chunk := await file.read(8192):
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE:
                    await out_file.close()
                    file_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"파일 크기가 너무 큽니다. 최대 {MAX_FILE_SIZE // 1024 // 1024}MB까지 지원됩니다",
                    )
                await out_file.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("file_upload_failed", file_id=file_id, error=str(e))
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="파일 저장 중 오류가 발생했습니다")

    # Get file info
    try:
        ppt_service = PPTService(str(file_path))
        slide_count = ppt_service.get_slide_count()
        texts = ppt_service.get_all_texts()

        logger.info(
            "file_upload_completed",
            file_id=file_id,
            slide_count=slide_count,
            text_count=len(texts),
        )
    except Exception as e:
        logger.error("file_processing_failed", file_id=file_id, error=str(e))
        file_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="PPT 파일 처리 중 오류가 발생했습니다. 파일이 손상되었거나 지원되지 않는 형식입니다."
        )

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
    logger.info(
        "translation_started",
        file_id=file_id,
        source=source_lang.value,
        target=target_lang.value,
        min_review_loops=min_review_loops,
    )

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            translation_status[file_id] = TranslationStatus(
                status="error",
                progress=0,
                total_slides=0,
                current_slide=0,
                review_loop=0,
                message="API 키가 설정되지 않았습니다",
            )
            return

        file_path = UPLOAD_DIR / f"{file_id}.pptx"
        output_path = UPLOAD_DIR / f"{file_id}_translated.pptx"

        ppt_service = PPTService(str(file_path))
        orchestrator = TranslationOrchestrator(api_key, min_review_loops)

        # Extract texts
        texts_data = ppt_service.get_all_texts()
        unique_texts = list(dict.fromkeys(item["text"] for item in texts_data))  # Preserve order
        total_texts = len(unique_texts)

        if total_texts == 0:
            translation_status[file_id] = TranslationStatus(
                status="completed",
                progress=100,
                total_slides=ppt_service.get_slide_count(),
                current_slide=0,
                review_loop=0,
                message="번역할 텍스트가 없습니다",
            )
            # Copy original file as output
            import shutil
            shutil.copy(file_path, output_path)
            return

        translation_status[file_id] = TranslationStatus(
            status="processing",
            progress=0,
            total_slides=ppt_service.get_slide_count(),
            current_slide=0,
            review_loop=0,
            message=f"{total_texts}개 텍스트 번역 시작...",
        )

        # Translate each unique text
        translations = {}
        for idx, text in enumerate(unique_texts):
            # Update status
            translation_status[file_id] = TranslationStatus(
                status="processing",
                progress=int((idx / total_texts) * 95),
                total_slides=ppt_service.get_slide_count(),
                current_slide=idx + 1,
                review_loop=0,
                message=f"텍스트 {idx + 1}/{total_texts} 번역 중...",
            )

            def update_review_status(iteration: int, message: str):
                translation_status[file_id] = TranslationStatus(
                    status="processing",
                    progress=int((idx / total_texts) * 95),
                    total_slides=ppt_service.get_slide_count(),
                    current_slide=idx + 1,
                    review_loop=iteration,
                    message=f"텍스트 {idx + 1}/{total_texts}: 리뷰 {iteration}회차",
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
            message="번역 결과를 PPT에 적용 중...",
        )

        ppt_service.apply_translations(translations, str(output_path))

        translation_status[file_id] = TranslationStatus(
            status="completed",
            progress=100,
            total_slides=ppt_service.get_slide_count(),
            current_slide=total_texts,
            review_loop=min_review_loops,
            message="번역이 완료되었습니다!",
        )

        logger.info(
            "translation_completed",
            file_id=file_id,
            texts_translated=total_texts,
        )

    except Exception as e:
        logger.error("translation_failed", file_id=file_id, error=str(e), exc_info=True)
        translation_status[file_id] = TranslationStatus(
            status="error",
            progress=0,
            total_slides=0,
            current_slide=0,
            review_loop=0,
            message="번역 중 오류가 발생했습니다. 다시 시도해주세요.",
        )


@app.post("/api/translate/{file_id}")
@limiter.limit("5/minute")
async def start_translation(
    request: Request,
    file_id: str,
    background_tasks: BackgroundTasks,
    source_language: str = Form(...),
    target_language: str = Form(...),
    min_review_loops: int = Form(5),
):
    """Start translation process."""
    file_id = validate_file_id(file_id)

    file_path = UPLOAD_DIR / f"{file_id}.pptx"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다")

    try:
        source_lang = Language(source_language)
        target_lang = Language(target_language)
    except ValueError:
        raise HTTPException(status_code=400, detail="지원하지 않는 언어입니다")

    if source_lang == target_lang:
        raise HTTPException(status_code=400, detail="원본 언어와 대상 언어가 같습니다")

    # Validate review loops
    min_review_loops = max(5, min(10, min_review_loops))

    # Initialize status
    translation_status[file_id] = TranslationStatus(
        status="queued",
        progress=0,
        total_slides=0,
        current_slide=0,
        review_loop=0,
        message="번역 대기 중...",
    )

    # Start background task
    background_tasks.add_task(
        process_translation,
        file_id,
        source_lang,
        target_lang,
        min_review_loops,
    )

    logger.info("translation_queued", file_id=file_id)
    return {"message": "번역이 시작되었습니다", "file_id": file_id}


@app.get("/api/status/{file_id}")
async def get_status(file_id: str):
    """Get translation status."""
    file_id = validate_file_id(file_id)

    if file_id not in translation_status:
        raise HTTPException(status_code=404, detail="번역 상태를 찾을 수 없습니다")

    return translation_status[file_id]


@app.get("/api/download/{file_id}")
async def download_file(file_id: str):
    """Download translated file."""
    file_id = validate_file_id(file_id)

    output_path = UPLOAD_DIR / f"{file_id}_translated.pptx"

    if not output_path.exists():
        raise HTTPException(status_code=404, detail="번역된 파일을 찾을 수 없습니다")

    logger.info("file_downloaded", file_id=file_id)

    return FileResponse(
        path=output_path,
        filename="translated.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


@app.delete("/api/file/{file_id}")
async def delete_file(file_id: str):
    """Delete uploaded and translated files."""
    file_id = validate_file_id(file_id)

    file_path = UPLOAD_DIR / f"{file_id}.pptx"
    output_path = UPLOAD_DIR / f"{file_id}_translated.pptx"

    if file_path.exists():
        file_path.unlink()
    if output_path.exists():
        output_path.unlink()

    if file_id in translation_status:
        del translation_status[file_id]

    logger.info("files_deleted", file_id=file_id)
    return {"message": "파일이 삭제되었습니다"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
