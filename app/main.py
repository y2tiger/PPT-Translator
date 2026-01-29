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
from app.agents.translator import TranslatorAgent
from app.agents.qa_agent import QAAgent

# QA loop settings
MAX_QA_ITERATIONS = 3

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
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set - translations will fail")
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
    api_key = os.environ.get("OPENAI_API_KEY")
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
            "api_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
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
):
    """Background task for translation processing with slide-based context."""
    logger.info(
        "translation_started",
        file_id=file_id,
        source=source_lang.value,
        target=target_lang.value,
    )

    try:
        api_key = os.environ.get("OPENAI_API_KEY")
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
        translator = TranslatorAgent(api_key)

        # Extract texts grouped by slide for context-aware translation
        texts_by_slide = ppt_service.get_texts_by_slide()
        total_slides = ppt_service.get_slide_count()
        slides_with_text = len(texts_by_slide)

        if slides_with_text == 0:
            translation_status[file_id] = TranslationStatus(
                status="completed",
                progress=100,
                total_slides=total_slides,
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
            total_slides=total_slides,
            current_slide=0,
            review_loop=0,
            message=f"{slides_with_text}개 슬라이드 번역 시작...",
        )

        # Translate slide by slide for better context
        all_translations = {}
        processed_slides = 0

        for slide_num, slide_texts in texts_by_slide.items():
            processed_slides += 1

            # Update status
            translation_status[file_id] = TranslationStatus(
                status="processing",
                progress=int((processed_slides / slides_with_text) * 70),
                total_slides=total_slides,
                current_slide=slide_num,
                review_loop=0,
                message=f"슬라이드 {slide_num}/{total_slides} 번역 중... ({len(slide_texts)}개 텍스트)",
            )

            # Translate all texts in this slide together for context
            slide_translations = await translator.translate_slide_batch(
                texts=slide_texts,
                source_lang=source_lang,
                target_lang=target_lang,
                slide_number=slide_num,
            )

            # Merge translations
            all_translations.update(slide_translations)

            logger.info(
                "slide_translated",
                file_id=file_id,
                slide_number=slide_num,
                text_count=len(slide_texts),
            )

        # QA Loop - Check and fix translation issues
        qa_agent = QAAgent(api_key)

        for qa_iteration in range(1, MAX_QA_ITERATIONS + 1):
            translation_status[file_id] = TranslationStatus(
                status="processing",
                progress=70 + (qa_iteration * 8),
                total_slides=total_slides,
                current_slide=total_slides,
                review_loop=qa_iteration,
                message=f"품질 검증 {qa_iteration}/{MAX_QA_ITERATIONS}회차...",
            )

            # Run QA analysis
            qa_result = await qa_agent.analyze_translations(
                original_texts=texts_by_slide,
                translations=all_translations,
                source_lang=source_lang,
                target_lang=target_lang,
            )

            logger.info(
                "qa_iteration_complete",
                file_id=file_id,
                iteration=qa_iteration,
                passed=qa_result.passed,
                issues_count=len(qa_result.issues),
            )

            if qa_result.passed:
                logger.info("qa_passed", file_id=file_id, iteration=qa_iteration)
                break

            # Get fixes for issues
            if qa_result.issues:
                translation_status[file_id] = TranslationStatus(
                    status="processing",
                    progress=70 + (qa_iteration * 8) + 4,
                    total_slides=total_slides,
                    current_slide=total_slides,
                    review_loop=qa_iteration,
                    message=f"품질 검증 {qa_iteration}회차: {len(qa_result.issues)}개 문제 수정 중...",
                )

                fixes = await qa_agent.suggest_fixes(
                    issues=qa_result.issues,
                    source_lang=source_lang,
                    target_lang=target_lang,
                )

                # Apply fixes to translations
                if fixes:
                    all_translations.update(fixes)
                    logger.info(
                        "fixes_applied",
                        file_id=file_id,
                        iteration=qa_iteration,
                        fix_count=len(fixes),
                    )

        # Apply translations
        translation_status[file_id] = TranslationStatus(
            status="processing",
            progress=95,
            total_slides=total_slides,
            current_slide=total_slides,
            review_loop=0,
            message="번역 결과를 PPT에 적용 중...",
        )

        ppt_service.apply_translations(all_translations, str(output_path))

        total_texts = sum(len(texts) for texts in texts_by_slide.values())
        translation_status[file_id] = TranslationStatus(
            status="completed",
            progress=100,
            total_slides=total_slides,
            current_slide=total_slides,
            review_loop=0,
            message="번역이 완료되었습니다!",
        )

        logger.info(
            "translation_completed",
            file_id=file_id,
            slides_translated=slides_with_text,
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
