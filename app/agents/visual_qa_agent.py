import base64
import json
import shutil
import subprocess
import tempfile
import structlog
from pathlib import Path
from dataclasses import dataclass, field
import openai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.models.schemas import Language, LANGUAGE_NAMES

logger = structlog.get_logger(__name__)


def check_libreoffice_available() -> bool:
    """Check if LibreOffice is available for PPT to image conversion."""
    try:
        result = subprocess.run(
            ["libreoffice", "--version"],
            capture_output=True,
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# Check availability at module load
LIBREOFFICE_AVAILABLE = check_libreoffice_available()
if not LIBREOFFICE_AVAILABLE:
    logger.warning("libreoffice_not_available",
                   message="Visual QA will be skipped - LibreOffice not installed")


@dataclass
class VisualIssue:
    """Visual issue found by comparing slides."""
    slide_number: int
    issue_type: str  # "untranslated", "overflow", "layout", "missing"
    description: str
    original_text: str
    suggestion: str
    severity: str = "warning"  # "critical", "warning", "info"


@dataclass
class SlideComparison:
    """Comparison data for a single slide."""
    slide_number: int
    original_image_path: str = ""
    translated_image_path: str = ""
    issues: list[VisualIssue] = field(default_factory=list)
    quality_score: int = 0
    algorithm_suggestions: list[str] = field(default_factory=list)


@dataclass
class VisualComparisonResult:
    """Result of visual comparison between original and translated slides."""
    slide_number: int
    issues: list[VisualIssue] = field(default_factory=list)
    quality_score: int = 0  # 0-100
    algorithm_suggestions: list[str] = field(default_factory=list)


@dataclass
class VisualQAReport:
    """Complete visual QA report."""
    iteration: int = 0
    total_slides: int = 0
    comparisons: list[VisualComparisonResult] = field(default_factory=list)
    slide_comparisons: list[SlideComparison] = field(default_factory=list)
    overall_score: int = 0
    critical_issues: list[VisualIssue] = field(default_factory=list)
    algorithm_improvements: list[str] = field(default_factory=list)
    texts_to_retranslate: list[str] = field(default_factory=list)
    texts_with_formatting_issues: list[str] = field(default_factory=list)  # FONT_SIZE, TRUNCATION issues


class VisualQAAgent:
    """Agent for visual comparison of original and translated PPT slides."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    def _ppt_to_pdf(self, ppt_path: str, output_dir: Path) -> Path | None:
        """Convert PPT to PDF using LibreOffice."""
        try:
            subprocess.run([
                "libreoffice", "--headless", "--convert-to", "pdf",
                "--outdir", str(output_dir), str(ppt_path)
            ], check=True, capture_output=True, timeout=120)

            ppt_name = Path(ppt_path).stem
            pdf_file = output_dir / f"{ppt_name}.pdf"

            if not pdf_file.exists():
                logger.error("pdf_conversion_failed", ppt_path=ppt_path)
                return None

            return pdf_file
        except Exception as e:
            logger.error("ppt_to_pdf_failed", error=str(e))
            return None

    def _get_pdf_page_count(self, pdf_path: Path) -> int:
        """Get the number of pages in a PDF."""
        try:
            result = subprocess.run([
                "pdfinfo", str(pdf_path)
            ], capture_output=True, text=True, timeout=30)

            for line in result.stdout.split('\n'):
                if line.startswith('Pages:'):
                    return int(line.split(':')[1].strip())
            return 0
        except Exception as e:
            logger.error("pdf_page_count_failed", error=str(e))
            return 0

    def _pdf_to_images_batch(self, pdf_path: Path, output_dir: Path,
                             first_page: int, last_page: int) -> list[Path]:
        """Convert a range of PDF pages to images."""
        try:
            # Using 100 DPI to reduce memory usage
            subprocess.run([
                "pdftoppm", "-png", "-r", "100",
                "-f", str(first_page), "-l", str(last_page),
                str(pdf_path), str(output_dir / "slide")
            ], check=True, capture_output=True, timeout=120)

            # Collect generated images
            images = sorted(output_dir.glob("slide-*.png"))
            logger.info("pdf_batch_converted", first=first_page, last=last_page, count=len(images))
            return images
        except Exception as e:
            logger.error("pdf_batch_conversion_failed", error=str(e))
            return []

    def _ppt_to_images(self, ppt_path: str, output_dir: Path) -> list[Path]:
        """Convert PPT to images using LibreOffice."""
        try:
            # First convert PPT to PDF using LibreOffice
            pdf_path = output_dir / "slides.pdf"

            subprocess.run([
                "libreoffice", "--headless", "--convert-to", "pdf",
                "--outdir", str(output_dir), str(ppt_path)
            ], check=True, capture_output=True, timeout=120)

            # Find the generated PDF
            ppt_name = Path(ppt_path).stem
            pdf_file = output_dir / f"{ppt_name}.pdf"

            if not pdf_file.exists():
                logger.error("pdf_conversion_failed", ppt_path=ppt_path)
                return []

            # Convert PDF to images using pdftoppm (from poppler-utils)
            # Using 100 DPI instead of 150 to reduce memory usage
            subprocess.run([
                "pdftoppm", "-png", "-r", "100",
                str(pdf_file), str(output_dir / "slide")
            ], check=True, capture_output=True, timeout=120)

            # Delete PDF immediately to free memory
            pdf_file.unlink(missing_ok=True)

            # Collect generated images
            images = sorted(output_dir.glob("slide-*.png"))
            logger.info("ppt_converted_to_images", count=len(images))
            return images

        except subprocess.TimeoutExpired:
            logger.error("conversion_timeout", ppt_path=ppt_path)
            return []
        except subprocess.CalledProcessError as e:
            logger.error("conversion_failed", error=e.stderr.decode() if e.stderr else str(e))
            return []
        except Exception as e:
            logger.error("conversion_error", error=str(e))
            return []

    def _image_to_base64(self, image_path: Path) -> str:
        """Convert image to base64 string."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIConnectionError)
        ),
    )
    async def _compare_slides_vision(
        self,
        original_image: Path,
        translated_image: Path,
        slide_number: int,
        source_lang: Language,
        target_lang: Language,
    ) -> VisualComparisonResult:
        """Use GPT-4o Vision to compare original and translated slides."""

        original_b64 = self._image_to_base64(original_image)
        translated_b64 = self._image_to_base64(translated_image)

        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        prompt = f"""Compare these two presentation slides. The first is the ORIGINAL in {source_name}, the second is the TRANSLATED version in {target_name}.

Analyze and identify:
1. **Untranslated text**: Any {source_name} text that remains in the translated version
2. **Text overflow**: Text that is cut off or extends beyond its container
3. **Layout issues**: Text positioning problems, overlapping, or misalignment
4. **Missing text**: Text present in original but completely missing in translation

For each issue found, provide:
- The original text (if visible)
- Description of the problem
- Suggested fix

Also provide:
- Overall quality score (0-100)
- Algorithm improvement suggestions - ONLY from the categories below:

SUGGESTION CATEGORIES (choose the most relevant):
- [EXTRACTION] Text not extracted: "특정 텍스트 '{{text}}' 추출 실패 - 슬라이드 {{N}}번의 {{위치}} 영역 확인 필요"
- [TRANSLATION] Translation quality: "번역 품질 개선 필요: '{{원문}}' → 현재 '{{번역}}', 제안: '{{더 나은 번역}}'"
- [FONT_SIZE] Text too long: "폰트 크기 축소 필요: '{{text}}' (원문 {{N}}자 → 번역 {{M}}자, {{ratio}}배 증가)"
- [TRUNCATION] Text cut off: "텍스트 잘림: '{{text}}' - 번역을 더 짧게 요약 필요"
- [GROUPED_SHAPE] Grouped object: "그룹 도형 내 텍스트 '{{text}}' 처리 실패 - 슬라이드 {{N}}번"
- [TABLE] Table cell: "테이블 셀 텍스트 '{{text}}' 처리 필요 - 행{{R}}/열{{C}}"
- [SPECIAL_CHAR] Special characters: "특수문자/기호 포함 텍스트 처리 필요: '{{text}}'"

Return as JSON:
{{
  "quality_score": 85,
  "issues": [
    {{
      "type": "untranslated",
      "original_text": "생산기술학교",
      "description": "Korean text not translated",
      "suggestion": "Force translate this text"
    }}
  ],
  "algorithm_suggestions": [
    "[EXTRACTION] 특정 텍스트 '에스엘' 추출 실패 - 슬라이드 1번의 상단 로고 영역 확인 필요",
    "[FONT_SIZE] 폰트 크기 축소 필요: 'Szkoła Technologii' (원문 5자 → 번역 18자, 3.6배 증가)"
  ]
}}"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=2000,
                timeout=120.0,  # 2 minute timeout for vision API
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{original_b64}",
                                    "detail": "high"
                                }
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{translated_b64}",
                                    "detail": "high"
                                }
                            }
                        ]
                    }
                ]
            )

            result_text = response.choices[0].message.content.strip()

            # Extract JSON from response
            if "```" in result_text:
                import re
                json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', result_text)
                if json_match:
                    result_text = json_match.group(1)

            result = json.loads(result_text)

            # Build comparison result
            issues = []
            for issue_data in result.get("issues", []):
                issues.append(VisualIssue(
                    slide_number=slide_number,
                    issue_type=issue_data.get("type", "unknown"),
                    description=issue_data.get("description", ""),
                    original_text=issue_data.get("original_text", ""),
                    suggestion=issue_data.get("suggestion", ""),
                    severity="critical" if issue_data.get("type") == "untranslated" else "warning"
                ))

            return VisualComparisonResult(
                slide_number=slide_number,
                issues=issues,
                quality_score=result.get("quality_score", 50),
                algorithm_suggestions=result.get("algorithm_suggestions", [])
            )

        except json.JSONDecodeError as e:
            logger.error("vision_json_parse_error", slide=slide_number, error=str(e))
            return VisualComparisonResult(slide_number=slide_number, quality_score=50)
        except Exception as e:
            logger.error("vision_comparison_failed", slide=slide_number, error=str(e))
            return VisualComparisonResult(slide_number=slide_number, quality_score=50)

    async def compare_presentations(
        self,
        original_ppt_path: str,
        translated_ppt_path: str,
        source_lang: Language,
        target_lang: Language,
        max_slides: int = 9999,  # Process all slides (batch processing handles memory)
        iteration: int = 1,
        output_dir: Path | None = None,  # Directory to save images for UI
        batch_size: int = 3,  # Process 3 slides at a time to reduce memory
        progress_callback: callable = None,  # Callback for progress updates
    ) -> VisualQAReport:
        """
        Compare original and translated presentations visually using batch processing.
        All slides are processed in batches of 3 to manage memory usage.

        Args:
            original_ppt_path: Path to original PPT
            translated_ppt_path: Path to translated PPT
            source_lang: Source language
            target_lang: Target language
            max_slides: Maximum slides to compare (default: all slides)
            iteration: Current iteration number
            output_dir: Directory to save comparison images (for UI display)
            batch_size: Number of slides to process at once (default: 3)
            progress_callback: Optional callback(batch_num, total_batches, slide_num, total_slides)

        Returns:
            VisualQAReport with issues and improvement suggestions
        """
        import gc

        # Check if LibreOffice is available
        if not LIBREOFFICE_AVAILABLE:
            logger.warning("visual_qa_skipped_no_libreoffice",
                           message="LibreOffice not available, skipping visual QA")
            return VisualQAReport(
                iteration=iteration,
                total_slides=-1,  # -1 indicates skipped (not failed)
                overall_score=-1,  # -1 indicates not available
                algorithm_improvements=["Visual QA skipped: LibreOffice not installed"],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            original_dir = tmpdir_path / "original"
            translated_dir = tmpdir_path / "translated"
            original_dir.mkdir()
            translated_dir.mkdir()

            # Step 1: Convert PPTs to PDFs (one time only)
            logger.info("converting_ppts_to_pdf")
            original_pdf = self._ppt_to_pdf(original_ppt_path, original_dir)
            translated_pdf = self._ppt_to_pdf(translated_ppt_path, translated_dir)

            if not original_pdf or not translated_pdf:
                logger.error("pdf_conversion_failed")
                return VisualQAReport(total_slides=0)

            # Step 2: Get page counts
            original_pages = self._get_pdf_page_count(original_pdf)
            translated_pages = self._get_pdf_page_count(translated_pdf)
            num_slides = min(original_pages, translated_pages, max_slides)

            if num_slides == 0:
                logger.error("no_pages_found")
                return VisualQAReport(total_slides=0)

            # Calculate total batches for progress reporting
            total_batches = (num_slides + batch_size - 1) // batch_size
            logger.info("batch_processing_start", total_slides=num_slides, batch_size=batch_size, total_batches=total_batches)

            # Create iteration directory for saving images if output_dir provided
            iter_dir = None
            if output_dir:
                iter_dir = output_dir / f"iteration_{iteration}"
                iter_dir.mkdir(parents=True, exist_ok=True)

            # Step 3: Process in batches
            comparisons = []
            all_issues = []
            all_suggestions = []
            total_score = 0
            slide_comparisons = []
            current_batch = 0

            for batch_start in range(1, num_slides + 1, batch_size):
                current_batch += 1
                batch_end = min(batch_start + batch_size - 1, num_slides)
                logger.info("processing_batch", batch=current_batch, total_batches=total_batches, batch_start=batch_start, batch_end=batch_end)

                # Call progress callback if provided
                if progress_callback:
                    try:
                        progress_callback(current_batch, total_batches, batch_start, num_slides)
                    except Exception as e:
                        logger.debug("progress_callback_error", error=str(e))

                # Create batch directories
                batch_orig_dir = tmpdir_path / f"batch_orig_{batch_start}"
                batch_trans_dir = tmpdir_path / f"batch_trans_{batch_start}"
                batch_orig_dir.mkdir(exist_ok=True)
                batch_trans_dir.mkdir(exist_ok=True)

                # Convert batch pages to images
                original_images = self._pdf_to_images_batch(
                    original_pdf, batch_orig_dir, batch_start, batch_end
                )
                translated_images = self._pdf_to_images_batch(
                    translated_pdf, batch_trans_dir, batch_start, batch_end
                )

                if not original_images or not translated_images:
                    logger.warning("batch_conversion_failed", batch_start=batch_start)
                    continue

                # Compare slides in this batch
                for i, (orig_img, trans_img) in enumerate(zip(original_images, translated_images)):
                    slide_num = batch_start + i
                    logger.info("comparing_slide", slide=slide_num, total=num_slides)

                    comparison = await self._compare_slides_vision(
                        orig_img,
                        trans_img,
                        slide_number=slide_num,
                        source_lang=source_lang,
                        target_lang=target_lang,
                    )

                    comparisons.append(comparison)
                    all_issues.extend(comparison.issues)
                    all_suggestions.extend(comparison.algorithm_suggestions)
                    total_score += comparison.quality_score

                    # Save images and create slide comparison
                    original_image_path = ""
                    translated_image_path = ""

                    if iter_dir:
                        # Copy images to persistent location
                        orig_dest = iter_dir / f"slide_{slide_num}_original.png"
                        trans_dest = iter_dir / f"slide_{slide_num}_translated.png"
                        shutil.copy(orig_img, orig_dest)
                        shutil.copy(trans_img, trans_dest)
                        original_image_path = str(orig_dest)
                        translated_image_path = str(trans_dest)

                    slide_comparisons.append(SlideComparison(
                        slide_number=slide_num,
                        original_image_path=original_image_path,
                        translated_image_path=translated_image_path,
                        issues=comparison.issues,
                        quality_score=comparison.quality_score,
                        algorithm_suggestions=comparison.algorithm_suggestions,
                    ))

                # Clean up batch images to free memory
                for img in original_images:
                    img.unlink(missing_ok=True)
                for img in translated_images:
                    img.unlink(missing_ok=True)
                shutil.rmtree(batch_orig_dir, ignore_errors=True)
                shutil.rmtree(batch_trans_dir, ignore_errors=True)
                gc.collect()
                logger.info("batch_cleanup_complete", batch_start=batch_start)

            # Clean up PDFs
            original_pdf.unlink(missing_ok=True)
            translated_pdf.unlink(missing_ok=True)

            # Calculate overall score
            processed_slides = len(comparisons)
            overall_score = total_score // processed_slides if processed_slides > 0 else 0

            # Extract critical issues
            critical_issues = [i for i in all_issues if i.severity == "critical"]

            # Extract texts to retranslate
            texts_to_retranslate = [
                i.original_text for i in all_issues
                if i.issue_type == "untranslated" and i.original_text
            ]

            # Extract texts with formatting issues (overflow, truncation, font_size)
            formatting_issue_types = {"overflow", "text_overflow", "truncation", "font_size", "layout"}
            texts_with_formatting_issues = list(set([
                i.original_text for i in all_issues
                if i.issue_type in formatting_issue_types and i.original_text
            ]))

            # Deduplicate suggestions
            unique_suggestions = list(set(all_suggestions))

            report = VisualQAReport(
                iteration=iteration,
                total_slides=processed_slides,
                comparisons=comparisons,
                slide_comparisons=slide_comparisons,
                overall_score=overall_score,
                critical_issues=critical_issues,
                algorithm_improvements=unique_suggestions,
                texts_to_retranslate=texts_to_retranslate,
                texts_with_formatting_issues=texts_with_formatting_issues,
            )

            logger.info(
                "visual_qa_complete",
                iteration=iteration,
                total_slides=processed_slides,
                overall_score=overall_score,
                critical_issues=len(critical_issues),
                suggestions=len(unique_suggestions),
            )

            return report

    async def get_retranslations(
        self,
        texts: list[str],
        source_lang: Language,
        target_lang: Language,
        context: str = "",
    ) -> dict[str, str]:
        """Get improved translations for problematic texts."""
        if not texts:
            return {}

        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        numbered_texts = "\n".join(f"[{i+1}] {text}" for i, text in enumerate(texts))

        prompt = f"""These texts were identified as poorly translated or untranslated in a presentation.
Provide high-quality, CONCISE translations from {source_name} to {target_name}.

Context: {context if context else "Business/corporate presentation"}

RULES:
1. Keep translations SHORT - suitable for presentation slides
2. Keep brand names unchanged
3. Return ONLY JSON mapping number to translation

Texts to translate:
{numbered_texts}

Return format: {{"1": "translation1", "2": "translation2"}}"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=2000,
                temperature=0.3,
                timeout=90.0,  # 90 second timeout
                messages=[{"role": "user", "content": prompt}]
            )

            result = response.choices[0].message.content.strip()

            if "```" in result:
                import re
                json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', result)
                if json_match:
                    result = json_match.group(1)

            translations_dict = json.loads(result)

            return {
                texts[int(k)-1]: v
                for k, v in translations_dict.items()
                if k.isdigit() and int(k) <= len(texts)
            }

        except Exception as e:
            logger.error("retranslation_failed", error=str(e))
            return {}
