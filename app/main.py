import gc
import logging
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

from app.models.schemas import (
    Language, TranslationStatus, LANGUAGE_NAMES,
    TranslationStyle, TRANSLATION_STYLE_NAMES, TRANSLATION_STYLE_DESCRIPTIONS,
    QAHistoryResponse, QAIterationResponse, SlideComparisonResponse, VisualIssueResponse
)
from app.services.ppt_service import PPTService
from app.agents.orchestrator import TranslationOrchestrator
from app.agents.translator import TranslatorAgent
from app.agents.qa_agent import QAAgent
from app.agents.diagnostic_agent import DiagnosticAgent, IssueSeverity
from app.agents.visual_qa_agent import VisualQAAgent
from app.utils.font_utils import (
    check_missing_fonts, create_libreoffice_font_substitution,
    get_available_fonts, apply_font_to_ppt, AVAILABLE_FONTS, DEFAULT_KOREAN_FONT
)

# QA loop settings
MAX_QA_ITERATIONS = 3
MAX_VISUAL_ITERATIONS = 2  # Visual comparison iterations
VISUAL_QA_QUALITY_THRESHOLD = 95  # Score threshold to pass (raised from 85)

# Set logging level to INFO to see font processing logs
logging.basicConfig(level=logging.INFO)

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

    # Set up LibreOffice font substitution
    try:
        font_config_path = create_libreoffice_font_substitution()
        if font_config_path:
            logger.info("libreoffice_font_substitution_configured", path=font_config_path)
    except Exception as e:
        logger.warning("libreoffice_font_config_failed", error=str(e))

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
qa_history: dict[str, list[QAIterationResponse]] = {}  # Store QA iterations per file
original_filenames: dict[str, str] = {}  # Store original filenames for download


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


@app.get("/api/fonts")
async def get_fonts():
    """Get available fonts for translation output."""
    return {
        "fonts": get_available_fonts(),
        "default": "pretendard",
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

        # Check fonts used in PPT
        font_info = check_missing_fonts(str(file_path))

        logger.info(
            "file_upload_completed",
            file_id=file_id,
            slide_count=slide_count,
            text_count=len(texts),
            fonts_used=font_info.get("used_fonts", []),
            fonts_missing=font_info.get("missing_fonts", []),
        )
    except Exception as e:
        import traceback
        error_detail = str(e)
        stack_trace = traceback.format_exc()
        logger.error("file_processing_failed", file_id=file_id, error=error_detail, traceback=stack_trace)
        file_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"PPT 파일 처리 중 오류가 발생했습니다: {error_detail[:200]}"
        )

    # Store original filename for download
    original_filenames[file_id] = file.filename

    return {
        "file_id": file_id,
        "filename": file.filename,
        "slide_count": slide_count,
        "text_count": len(texts),
        "font_info": font_info,
    }


async def process_translation(
    file_id: str,
    source_lang: Language,
    target_lang: Language,
    target_font: str | None = None,
    visual_qa_iterations: int = 2,
    translation_style: TranslationStyle = TranslationStyle.TECHNICAL,
):
    """Background task for translation processing with slide-based context."""
    logger.info(
        "translation_started",
        file_id=file_id,
        source=source_lang.value,
        target=target_lang.value,
        target_font=target_font or DEFAULT_KOREAN_FONT,
        visual_qa_iterations=visual_qa_iterations,
        translation_style=translation_style.value,
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
                translation_style=translation_style,
            )

            # Merge translations
            all_translations.update(slide_translations)

            logger.info(
                "slide_translated",
                file_id=file_id,
                slide_number=slide_num,
                text_count=len(slide_texts),
            )

        # Diagnostic & QA Loop - Analyze pipeline and fix issues
        diagnostic_agent = DiagnosticAgent(api_key)
        applied_texts = {text: False for texts in texts_by_slide.values() for text in texts}

        for qa_iteration in range(1, MAX_QA_ITERATIONS + 1):
            translation_status[file_id] = TranslationStatus(
                status="processing",
                progress=70 + (qa_iteration * 8),
                total_slides=total_slides,
                current_slide=total_slides,
                review_loop=qa_iteration,
                message=f"품질 진단 {qa_iteration}/{MAX_QA_ITERATIONS}회차...",
            )

            # Run diagnostic analysis
            diagnostic_report = await diagnostic_agent.analyze_pipeline(
                extracted_texts=texts_by_slide,
                translations=all_translations,
                applied_texts=applied_texts,
                source_lang=source_lang,
                target_lang=target_lang,
            )

            logger.info(
                "diagnostic_iteration_complete",
                file_id=file_id,
                iteration=qa_iteration,
                summary=diagnostic_report.summary,
                translation_rate=f"{diagnostic_report.translation_rate:.1f}%",
                issues_count=len(diagnostic_report.issues),
                recommendations=diagnostic_report.recommendations,
            )

            # Check if we have critical issues to fix
            critical_issues = [i for i in diagnostic_report.issues
                              if i.severity in (IssueSeverity.CRITICAL, IssueSeverity.WARNING)]

            if not critical_issues:
                logger.info("diagnostic_passed", file_id=file_id, iteration=qa_iteration)
                break

            # Get targeted fixes for issues
            translation_status[file_id] = TranslationStatus(
                status="processing",
                progress=70 + (qa_iteration * 8) + 4,
                total_slides=total_slides,
                current_slide=total_slides,
                review_loop=qa_iteration,
                message=f"품질 진단 {qa_iteration}회차: {len(critical_issues)}개 문제 수정 중...",
            )

            fixes = await diagnostic_agent.get_targeted_fixes(
                issues=critical_issues,
                source_lang=source_lang,
                target_lang=target_lang,
            )

            # Apply fixes to translations
            if fixes:
                all_translations.update(fixes)
                logger.info(
                    "diagnostic_fixes_applied",
                    file_id=file_id,
                    iteration=qa_iteration,
                    fix_count=len(fixes),
                )
            else:
                # No fixes available, break to avoid infinite loop
                logger.warning(
                    "no_fixes_available",
                    file_id=file_id,
                    iteration=qa_iteration,
                    remaining_issues=len(critical_issues),
                )
                break

        # Visual QA Loop - Compare images and iteratively improve (optional)
        qa_history[file_id] = []
        best_score = 0  # Initialize outside to avoid NameError when visual_qa_iterations <= 0
        last_format_adjustments = []  # Store adjustments from last iteration for final application

        if visual_qa_iterations <= 0:
            logger.info("visual_qa_disabled", file_id=file_id)
        else:
            visual_qa_agent = VisualQAAgent(api_key)
            max_iterations = min(visual_qa_iterations, 3)  # Cap at 3

            # Create directory for QA images
            qa_images_dir = UPLOAD_DIR / f"{file_id}_qa"
            qa_images_dir.mkdir(exist_ok=True)

            # IMPORTANT: Create a backup copy of the original Korean PPT BEFORE any translations
            # This ensures we have an unmodified original for Visual QA comparison
            # ALWAYS overwrite to ensure fresh backup (in case of retry with same file_id)
            import shutil
            import hashlib
            original_backup_path = UPLOAD_DIR / f"{file_id}_original_backup.pptx"

            # Log source file content BEFORE backup
            source_hash = hashlib.md5(file_path.read_bytes()).hexdigest()[:8]
            source_texts = ppt_service.get_all_texts()[:5]  # First 5 texts for verification
            logger.info(
                "backup_source_verification",
                file_id=file_id,
                source_path=str(file_path),
                source_hash=source_hash,
                sample_texts=source_texts,
            )

            shutil.copy(file_path, original_backup_path)

            # Verify backup content matches source
            backup_hash = hashlib.md5(original_backup_path.read_bytes()).hexdigest()[:8]
            backup_service = PPTService(str(original_backup_path))
            backup_texts = backup_service.get_all_texts()[:5]
            logger.info(
                "original_backup_created",
                file_id=file_id,
                backup_path=str(original_backup_path),
                source_path=str(file_path),
                source_hash=source_hash,
                backup_hash=backup_hash,
                hashes_match=(source_hash == backup_hash),
                backup_sample_texts=backup_texts,
            )

            # For Visual QA comparison:
            # - Original: keeps its original fonts (shows actual original PPT)
            # - Translated: gets target font applied (shows final output appearance)
            translated_for_qa = UPLOAD_DIR / f"{file_id}_translated_for_qa.pptx"

            for visual_iteration in range(1, max_iterations + 1):
                # Apply current translations
                translation_status[file_id] = TranslationStatus(
                    status="processing",
                    progress=70 + (visual_iteration * 5),
                    total_slides=total_slides,
                    current_slide=total_slides,
                    review_loop=visual_iteration,
                    message=f"시각적 품질 검증 {visual_iteration}/{max_iterations}회차: PPT 생성 중...",
                )

                # Re-create PPT service for fresh state
                ppt_service = PPTService(str(file_path))
                _, applied_tracker = ppt_service.apply_translations(all_translations, str(output_path))

                # Apply target font to translated output for fair comparison
                if target_font:
                    shutil.copy(output_path, translated_for_qa)
                    apply_font_to_ppt(str(translated_for_qa), str(translated_for_qa), target_font)
                else:
                    shutil.copy(output_path, translated_for_qa)

                # Log paths being used for Visual QA comparison
                backup_hash = hashlib.md5(original_backup_path.read_bytes()).hexdigest()[:8] if original_backup_path.exists() else "none"
                trans_hash = hashlib.md5(translated_for_qa.read_bytes()).hexdigest()[:8] if translated_for_qa.exists() else "none"

                # Verify backup still contains original content (not translated)
                backup_verify_service = PPTService(str(original_backup_path))
                backup_verify_texts = backup_verify_service.get_all_texts()[:3]

                logger.info(
                    "visual_qa_paths",
                    file_id=file_id,
                    iteration=visual_iteration,
                    original_path=str(original_backup_path),
                    translated_for_qa=str(translated_for_qa),
                    target_font_applied=bool(target_font),
                    backup_exists=original_backup_path.exists(),
                    translated_exists=translated_for_qa.exists(),
                    backup_hash=backup_hash,
                    translated_hash=trans_hash,
                    files_are_same=(backup_hash == trans_hash),
                    backup_sample_texts=backup_verify_texts,
                )

                # Original: original fonts, Translated: target font applied

                # Visual comparison
                translation_status[file_id] = TranslationStatus(
                    status="processing",
                    progress=70 + ((visual_iteration - 1) * 25 // max_iterations),
                    total_slides=total_slides,
                    current_slide=total_slides,
                    review_loop=visual_iteration,
                    message=f"시각적 품질 검증 {visual_iteration}/{max_iterations}회차: 이미지 비교 준비 중...",
                )

                # Progress callback for Visual QA batch processing
                # Visual QA uses 70-95% of progress bar (25% total, divided by iterations)
                iteration_progress_range = 25 // max_iterations  # e.g., 25 for 1 iteration, 8 for 3 iterations
                iteration_base_progress = 70 + ((visual_iteration - 1) * iteration_progress_range)

                def visual_qa_progress(
                    batch_num, total_batches, slide_num, total_qa_slides,
                    _base=iteration_base_progress, _range=iteration_progress_range,
                    _iter=visual_iteration, _max_iter=max_iterations
                ):
                    # Calculate progress within this iteration based on batch completion
                    batch_progress = (batch_num / total_batches) * _range
                    current_progress = int(_base + batch_progress)
                    translation_status[file_id] = TranslationStatus(
                        status="processing",
                        progress=min(95, current_progress),  # Cap at 95% until final step
                        total_slides=total_slides,
                        current_slide=total_slides,
                        review_loop=_iter,
                        message=f"시각적 품질 검증 {_iter}/{_max_iter}회차: 배치 {batch_num}/{total_batches} (슬라이드 {slide_num}-{min(slide_num+2, total_qa_slides)}/{total_qa_slides})",
                    )

                try:
                    # Original: keeps original fonts, Translated: target font applied
                    visual_report = await visual_qa_agent.compare_presentations(
                        original_ppt_path=str(original_backup_path),
                        translated_ppt_path=str(translated_for_qa),
                        source_lang=source_lang,
                        target_lang=target_lang,
                        max_slides=total_slides,  # Process all slides with batch processing
                        iteration=visual_iteration,
                        output_dir=qa_images_dir,
                        progress_callback=visual_qa_progress,
                    )

                    # Check if visual QA was skipped (LibreOffice not available)
                    if visual_report.overall_score == -1:
                        logger.warning(
                            "visual_qa_not_available",
                            file_id=file_id,
                            message="Visual QA skipped - LibreOffice not installed",
                        )
                        # Store a placeholder QA iteration for UI display
                        qa_history[file_id].append(QAIterationResponse(
                            iteration=visual_iteration,
                            overall_score=-1,
                            total_slides=0,
                            critical_issues_count=0,
                            texts_retranslated=0,
                            algorithm_improvements=["Visual QA is not available (LibreOffice not installed)"],
                            slide_comparisons=[],
                        ))
                        break  # Exit loop - can't do visual QA without LibreOffice

                    logger.info(
                        "visual_qa_iteration",
                        file_id=file_id,
                        iteration=visual_iteration,
                        score=visual_report.overall_score,
                        critical_issues=len(visual_report.critical_issues),
                        improvements=visual_report.algorithm_improvements,
                    )

                    # Store QA iteration for UI display
                    slide_comparisons_response = []
                    for sc in visual_report.slide_comparisons:
                        # Convert file paths to URLs
                        orig_url = f"/api/qa-image/{file_id}/{visual_iteration}/{sc.slide_number}/original"
                        trans_url = f"/api/qa-image/{file_id}/{visual_iteration}/{sc.slide_number}/translated"

                        issues_response = [
                            VisualIssueResponse(
                                slide_number=issue.slide_number,
                                issue_type=issue.issue_type or "",
                                description=issue.description or "",
                                original_text=issue.original_text or "",
                                suggestion=issue.suggestion or "",
                                severity=issue.severity or "warning",
                            )
                            for issue in sc.issues
                        ]

                        slide_comparisons_response.append(SlideComparisonResponse(
                            slide_number=sc.slide_number,
                            original_image_url=orig_url,
                            translated_image_url=trans_url,
                            issues=issues_response,
                            quality_score=sc.quality_score,
                            suggestions=sc.algorithm_suggestions,
                        ))

                    # Check for formatting issues (FONT_SIZE, TRUNCATION, etc.)
                    has_formatting_issues = len(getattr(visual_report, 'texts_with_formatting_issues', [])) > 0
                    formatting_issues_count = len(getattr(visual_report, 'texts_with_formatting_issues', []))

                    qa_iteration_response = QAIterationResponse(
                        iteration=visual_iteration,
                        overall_score=visual_report.overall_score,
                        total_slides=visual_report.total_slides,
                        critical_issues_count=len(visual_report.critical_issues),
                        texts_retranslated=len(visual_report.texts_to_retranslate),
                        formatting_issues_count=formatting_issues_count,
                        format_adjustments_applied=len(visual_report.format_adjustments),
                        algorithm_improvements=visual_report.algorithm_improvements,
                        slide_comparisons=slide_comparisons_response,
                    )
                    qa_history[file_id].append(qa_iteration_response)

                    best_score = max(best_score, visual_report.overall_score)

                    if has_formatting_issues:
                        logger.info(
                            "formatting_issues_detected",
                            file_id=file_id,
                            iteration=visual_iteration,
                            count=formatting_issues_count,
                            texts=visual_report.texts_with_formatting_issues[:5],  # Log first 5
                        )

                    # Check if quality is good enough AND no formatting issues
                    if visual_report.overall_score >= VISUAL_QA_QUALITY_THRESHOLD and not has_formatting_issues:
                        logger.info(
                            "visual_qa_passed",
                            file_id=file_id,
                            iteration=visual_iteration,
                            score=visual_report.overall_score,
                        )
                        break

                    # If score is good but has formatting issues, log and continue to apply fixes
                    if visual_report.overall_score >= VISUAL_QA_QUALITY_THRESHOLD and has_formatting_issues:
                        logger.info(
                            "visual_qa_continuing_for_formatting",
                            file_id=file_id,
                            iteration=visual_iteration,
                            score=visual_report.overall_score,
                            formatting_issues=formatting_issues_count,
                        )

                    # Get retranslations for problematic texts
                    has_work_to_do = False

                    if visual_report.texts_to_retranslate:
                        # IMPORTANT: Filter texts - only retranslate if they exist in original extraction
                        # This prevents retranslating already-translated text (e.g., Polish back to Korean)
                        original_texts_set = set(text for texts in texts_by_slide.values() for text in texts)
                        valid_texts_to_retranslate = [
                            t for t in visual_report.texts_to_retranslate
                            if t in original_texts_set
                        ]

                        # Log filtering results
                        filtered_out = len(visual_report.texts_to_retranslate) - len(valid_texts_to_retranslate)
                        if filtered_out > 0:
                            logger.info(
                                "retranslation_filtered",
                                file_id=file_id,
                                original_count=len(visual_report.texts_to_retranslate),
                                valid_count=len(valid_texts_to_retranslate),
                                filtered_out=filtered_out,
                                filtered_texts=[t for t in visual_report.texts_to_retranslate if t not in original_texts_set][:5],
                            )

                        if valid_texts_to_retranslate:
                            translation_status[file_id] = TranslationStatus(
                                status="processing",
                                progress=70 + (visual_iteration * 5) + 4,
                                total_slides=total_slides,
                                current_slide=total_slides,
                                review_loop=visual_iteration,
                                message=f"시각적 품질 검증 {visual_iteration}회차: {len(valid_texts_to_retranslate)}개 텍스트 재번역 중...",
                            )

                            retranslations = await visual_qa_agent.get_retranslations(
                                texts=valid_texts_to_retranslate,
                                source_lang=source_lang,
                                target_lang=target_lang,
                            )

                            if retranslations:
                                all_translations.update(retranslations)
                                has_work_to_do = True
                                logger.info(
                                    "visual_retranslations_applied",
                                    file_id=file_id,
                                    iteration=visual_iteration,
                                    count=len(retranslations),
                                )

                    # Apply format adjustments (alignment, font size) from Visual QA
                    if visual_report.format_adjustments:
                        translation_status[file_id] = TranslationStatus(
                            status="processing",
                            progress=70 + (visual_iteration * 5) + 3,
                            total_slides=total_slides,
                            current_slide=total_slides,
                            review_loop=visual_iteration,
                            message=f"시각적 품질 검증 {visual_iteration}회차: {len(visual_report.format_adjustments)}개 포맷 조정 적용 중...",
                        )

                        # Convert FormatAdjustment objects to dicts for apply_adjustments
                        # IMPORTANT: Use translated text (not original) to find text in PPT
                        adjustments_to_apply = []
                        for adj in visual_report.format_adjustments:
                            # Look up the translated text from the original
                            translated_text = all_translations.get(adj.text, adj.text)
                            adjustments_to_apply.append({
                                "slide_number": adj.slide_number,
                                "text": translated_text,  # Use translated text to find in PPT
                                "original_text": adj.text,  # Keep original for logging
                                "adjustment_type": adj.adjustment_type,
                                "target_value": adj.target_value,
                            })
                            logger.info(
                                "format_adjustment_mapped",
                                original=adj.text[:30],
                                translated=translated_text[:30],
                                type=adj.adjustment_type,
                                target=adj.target_value,
                            )

                        # Apply adjustments to the translated PPT
                        _, applied_count = ppt_service.apply_adjustments(
                            adjustments_to_apply, str(translated_for_qa)
                        )

                        # Store adjustments for final application (important for 1-iteration case)
                        last_format_adjustments = adjustments_to_apply

                        if applied_count > 0:
                            has_work_to_do = True
                            logger.info(
                                "format_adjustments_applied",
                                file_id=file_id,
                                iteration=visual_iteration,
                                requested=len(visual_report.format_adjustments),
                                applied=applied_count,
                            )

                    # If we have formatting issues, that counts as "work to do" for next iteration
                    # The next iteration will re-apply translations with the already-aggressive font reduction
                    if has_formatting_issues:
                        has_work_to_do = True
                        translation_status[file_id] = TranslationStatus(
                            status="processing",
                            progress=70 + (visual_iteration * 5) + 4,
                            total_slides=total_slides,
                            current_slide=total_slides,
                            review_loop=visual_iteration,
                            message=f"시각적 품질 검증 {visual_iteration}회차: {formatting_issues_count}개 포맷 이슈 재처리 중...",
                        )

                    # Decide whether to continue iterating
                    # Continue if: we have work to do OR (score is below threshold AND not last iteration)
                    is_last_iteration = (visual_iteration >= max_iterations)
                    score_below_threshold = (visual_report.overall_score < VISUAL_QA_QUALITY_THRESHOLD)

                    if not has_work_to_do and (is_last_iteration or not score_below_threshold):
                        # No improvements possible and either last iteration or score is good enough
                        logger.info(
                            "visual_qa_iteration_complete",
                            file_id=file_id,
                            iteration=visual_iteration,
                            score=visual_report.overall_score,
                            threshold=VISUAL_QA_QUALITY_THRESHOLD,
                            is_last=is_last_iteration,
                        )
                        break
                    elif not has_work_to_do and score_below_threshold and not is_last_iteration:
                        # No specific improvements but score is still low - continue to next iteration
                        logger.info(
                            "continuing_despite_no_work",
                            file_id=file_id,
                            iteration=visual_iteration,
                            score=visual_report.overall_score,
                            reason="score below threshold, user requested more iterations",
                        )

                except Exception as visual_error:
                    logger.warning(
                        "visual_qa_skipped",
                        file_id=file_id,
                        iteration=visual_iteration,
                        error=str(visual_error),
                    )
                    # Continue without visual QA if it fails (e.g., LibreOffice not installed)
                    break

            # Cleanup temporary QA files
            if translated_for_qa.exists():
                translated_for_qa.unlink()

            # Free memory after Visual QA loop
            gc.collect()
            logger.info("memory_freed_after_visual_qa", file_id=file_id)

        # Free memory after Visual QA loop (or if skipped)
        gc.collect()
        logger.info("memory_freed_after_visual_qa", file_id=file_id)

        # Final application
        translation_status[file_id] = TranslationStatus(
            status="processing",
            progress=95,
            total_slides=total_slides,
            current_slide=total_slides,
            review_loop=0,
            message="최종 번역 결과 적용 중...",
        )

        # Apply final translations
        ppt_service = PPTService(str(file_path))
        _, applied_tracker = ppt_service.apply_translations(all_translations, str(output_path))

        # Apply target font if specified
        if target_font:
            apply_font_to_ppt(str(output_path), str(output_path), target_font)
            logger.info("font_applied", file_id=file_id, font=target_font)

        # Apply format adjustments from Visual QA to final output
        if last_format_adjustments:
            final_ppt_service = PPTService(str(output_path))
            _, adj_applied = final_ppt_service.apply_adjustments(last_format_adjustments, str(output_path))
            logger.info(
                "final_format_adjustments_applied",
                file_id=file_id,
                requested=len(last_format_adjustments),
                applied=adj_applied,
            )

        # Log final summary
        applied_count = sum(1 for v in applied_tracker.values() if v)
        logger.info(
            "final_application_summary",
            file_id=file_id,
            total_translations=len(all_translations),
            applied=applied_count,
            failed=len(applied_tracker) - applied_count,
            visual_score=best_score,
        )

        total_texts = sum(len(texts) for texts in texts_by_slide.values())

        # Set final message based on whether visual QA was available
        if best_score > 0:
            final_message = f"번역 완료! (품질 점수: {best_score}/100)"
        else:
            final_message = "번역 완료! (시각적 QA 불가 - LibreOffice 미설치)"

        translation_status[file_id] = TranslationStatus(
            status="completed",
            progress=100,
            total_slides=total_slides,
            current_slide=total_slides,
            review_loop=0,
            message=final_message,
        )

        logger.info(
            "translation_completed",
            file_id=file_id,
            slides_translated=slides_with_text,
            texts_translated=total_texts,
            visual_quality_score=best_score,
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
    target_font: str = Form("pretendard"),
    visual_qa_iterations: str = Form("2"),
    translation_style: str = Form("technical"),
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

    # Parse translation style
    try:
        style = TranslationStyle(translation_style)
    except ValueError:
        style = TranslationStyle.TECHNICAL

    if source_lang == target_lang:
        raise HTTPException(status_code=400, detail="원본 언어와 대상 언어가 같습니다")

    # Validate font selection
    if target_font not in AVAILABLE_FONTS:
        target_font = "pretendard"

    # Get actual font name from selection
    font_name = AVAILABLE_FONTS[target_font]["name"]

    # Initialize status
    translation_status[file_id] = TranslationStatus(
        status="queued",
        progress=0,
        total_slides=0,
        current_slide=0,
        review_loop=0,
        message="번역 대기 중...",
    )

    # Parse visual_qa_iterations (form sends string)
    try:
        qa_iterations = int(visual_qa_iterations)
        qa_iterations = max(0, min(3, qa_iterations))  # Clamp between 0-3
    except ValueError:
        qa_iterations = 2  # Default

    # Start background task
    background_tasks.add_task(
        process_translation,
        file_id,
        source_lang,
        target_lang,
        font_name,
        qa_iterations,
        style,
    )

    logger.info("translation_queued", file_id=file_id, target_font=target_font, visual_qa_iterations=qa_iterations, style=style.value)
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

    # Get original filename and create translated filename
    original_name = original_filenames.get(file_id, "translated.pptx")
    if original_name.lower().endswith(".pptx"):
        download_name = original_name[:-5] + "_translated.pptx"
    else:
        download_name = original_name + "_translated.pptx"

    logger.info("file_downloaded", file_id=file_id, download_name=download_name)

    return FileResponse(
        path=output_path,
        filename=download_name,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


@app.get("/api/qa-history/{file_id}")
async def get_qa_history(file_id: str):
    """Get QA iteration history for a file."""
    file_id = validate_file_id(file_id)

    if file_id not in qa_history:
        raise HTTPException(status_code=404, detail="QA 기록을 찾을 수 없습니다")

    iterations = qa_history[file_id]
    final_score = iterations[-1].overall_score if iterations else 0

    return QAHistoryResponse(
        file_id=file_id,
        iterations=iterations,
        final_score=final_score,
    )


@app.get("/api/qa-image/{file_id}/{iteration}/{slide_number}/{image_type}")
async def get_qa_image(file_id: str, iteration: int, slide_number: int, image_type: str):
    """Get a QA comparison image."""
    file_id = validate_file_id(file_id)

    if image_type not in ("original", "translated"):
        raise HTTPException(status_code=400, detail="Invalid image type")

    qa_dir = UPLOAD_DIR / f"{file_id}_qa" / f"iteration_{iteration}"
    image_path = qa_dir / f"slide_{slide_number}_{image_type}.png"

    if not image_path.exists():
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다")

    return FileResponse(path=image_path, media_type="image/png")


@app.delete("/api/file/{file_id}")
async def delete_file(file_id: str):
    """Delete uploaded and translated files."""
    file_id = validate_file_id(file_id)

    file_path = UPLOAD_DIR / f"{file_id}.pptx"
    output_path = UPLOAD_DIR / f"{file_id}_translated.pptx"
    original_backup_path = UPLOAD_DIR / f"{file_id}_original_backup.pptx"
    translated_for_qa = UPLOAD_DIR / f"{file_id}_translated_for_qa.pptx"
    qa_dir = UPLOAD_DIR / f"{file_id}_qa"

    if file_path.exists():
        file_path.unlink()
    if output_path.exists():
        output_path.unlink()
    if original_backup_path.exists():
        original_backup_path.unlink()
    if translated_for_qa.exists():
        translated_for_qa.unlink()
    if qa_dir.exists():
        import shutil
        shutil.rmtree(qa_dir)

    if file_id in translation_status:
        del translation_status[file_id]
    if file_id in qa_history:
        del qa_history[file_id]
    if file_id in original_filenames:
        del original_filenames[file_id]

    logger.info("files_deleted", file_id=file_id)
    return {"message": "파일이 삭제되었습니다"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
