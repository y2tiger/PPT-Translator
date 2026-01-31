import structlog
from pptx import Presentation
from pptx.shapes.group import GroupShape
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Pt
from pptx.oxml.ns import qn
from typing import Generator

logger = structlog.get_logger(__name__)


def _reset_character_spacing(run):
    """
    Reset character spacing to normal (0) for a run.
    Korean text often uses condensed spacing which doesn't work well with Latin characters.
    """
    try:
        # Access the underlying XML element
        rPr = run._r.get_or_add_rPr()
        # Remove the 'spc' (spacing) attribute if it exists
        # spc is in hundredths of a point, negative = condensed, positive = expanded
        if rPr.get(qn('a:spc')) is not None:
            del rPr.attrib[qn('a:spc')]
        # Also set it explicitly to 0 for safety
        rPr.set(qn('a:spc'), '0')
    except Exception as e:
        logger.debug("reset_spacing_failed", error=str(e))


class PPTService:
    """Service for handling PowerPoint file operations."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.presentation = Presentation(file_path)

    def _extract_from_shape(self, shape, slide_idx: int, depth: int = 0) -> Generator[dict, None, None]:
        """Extract text from a single shape at paragraph level for better context."""
        # Handle group shapes recursively (up to depth 10 to prevent infinite loops)
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    yield from self._extract_from_shape(child_shape, slide_idx, depth + 1)
            except Exception as e:
                logger.debug("group_extraction_error", error=str(e), shape_id=getattr(shape, 'shape_id', 'unknown'))
            # Don't return here - group shapes might also have text frames

        # Handle text frames - extract at PARAGRAPH level (not run level)
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            try:
                for paragraph in shape.text_frame.paragraphs:
                    # Get full paragraph text (combines all runs)
                    para_text = paragraph.text.strip()
                    if para_text:
                        yield {
                            "slide_number": slide_idx,
                            "shape_id": shape.shape_id,
                            "text": para_text,
                            "shape_type": str(getattr(shape, 'shape_type', 'unknown')),
                        }
            except Exception as e:
                logger.debug("text_frame_extraction_error", error=str(e))

        # Handle tables - extract at cell paragraph level for better matching
        if hasattr(shape, 'has_table') and shape.has_table:
            try:
                for row_idx, row in enumerate(shape.table.rows):
                    for col_idx, cell in enumerate(row.cells):
                        for paragraph in cell.text_frame.paragraphs:
                            para_text = paragraph.text.strip()
                            if para_text:
                                yield {
                                    "slide_number": slide_idx,
                                    "shape_id": shape.shape_id,
                                    "is_table": True,
                                    "row": row_idx,
                                    "col": col_idx,
                                    "text": para_text,
                                }
            except Exception as e:
                logger.debug("table_extraction_error", error=str(e))

        # Handle placeholders (titles, subtitles, content) - for debugging only
        # Note: accessing placeholder_format can raise "shape is not a placeholder" exception
        try:
            if hasattr(shape, 'placeholder_format') and shape.placeholder_format is not None:
                if hasattr(shape, 'text') and shape.text.strip():
                    logger.debug("placeholder_found",
                                placeholder_type=str(shape.placeholder_format.type),
                                text_preview=shape.text[:30] if shape.text else "")
        except Exception:
            pass  # Not a placeholder, ignore

    def extract_texts(self) -> Generator[dict, None, None]:
        """Extract all text elements from the presentation."""
        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            for shape in slide.shapes:
                yield from self._extract_from_shape(shape, slide_idx)

    def get_all_texts(self) -> list[dict]:
        """Get all texts as a list."""
        return list(self.extract_texts())

    def get_texts_by_slide(self) -> dict[int, list[str]]:
        """Get texts grouped by slide number for context-aware translation."""
        texts_by_slide: dict[int, list[str]] = {}

        for item in self.extract_texts():
            slide_num = item["slide_number"]
            text = item["text"]

            if slide_num not in texts_by_slide:
                texts_by_slide[slide_num] = []

            # Avoid duplicates within the same slide
            if text not in texts_by_slide[slide_num]:
                texts_by_slide[slide_num].append(text)
                # Debug: log each extracted text
                logger.debug(
                    "text_extracted",
                    slide=slide_num,
                    shape_id=item.get("shape_id"),
                    text_preview=text[:50] if len(text) > 50 else text,
                    is_table=item.get("is_table", False),
                )

        # Log summary
        total_texts = sum(len(texts) for texts in texts_by_slide.values())
        logger.info(
            "extraction_complete",
            total_slides=len(texts_by_slide),
            total_texts=total_texts,
        )

        return texts_by_slide

    def get_slide_count(self) -> int:
        """Return the number of slides."""
        return len(self.presentation.slides)

    def _calculate_font_size_ratio(self, original: str, translated: str) -> float:
        """Calculate font size ratio based on text length difference.

        Korean text is typically more compact than European languages.
        Polish translations are often 1.5-2.5x longer than Korean originals.
        """
        if not original or not translated:
            return 1.0

        # Calculate length ratio
        len_ratio = len(translated) / len(original)

        # If translated text is longer, reduce font size proportionally
        if len_ratio > 1.0:
            # More aggressive reduction formula
            # For len_ratio 1.5: ~0.82 (82%)
            # For len_ratio 2.0: ~0.71 (71%)
            # For len_ratio 3.0: ~0.58 (58%)
            # For len_ratio 4.0: ~0.50 (50%)
            size_ratio = 1.0 / (len_ratio ** 0.5)

            # Cap at 50% minimum (very aggressive but prevents unreadable text)
            size_ratio = max(0.5, size_ratio)

            # For short original text (likely titles/headers), be less aggressive
            if len(original) < 10:
                size_ratio = max(0.7, size_ratio)

            return size_ratio

        return 1.0

    def _apply_to_shape(
        self,
        shape,
        translations: dict[str, str],
        applied_tracker: dict[str, bool],
        target_font: str = "Arial",
        depth: int = 0
    ):
        """Apply translations to a single shape at paragraph level."""
        # Handle group shapes recursively (up to depth 10)
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    self._apply_to_shape(child_shape, translations, applied_tracker, target_font, depth + 1)
            except Exception as e:
                logger.debug("group_apply_error", error=str(e))
            # Don't return - continue to check for text frames

        # Handle text frames - apply at PARAGRAPH level
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            # Enable auto-fit for text to shrink if needed
            try:
                shape.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            except Exception:
                pass  # Some shapes don't support auto_size

            for paragraph in shape.text_frame.paragraphs:
                para_text = paragraph.text.strip()
                if para_text in translations:
                    translated = translations[para_text]
                    logger.debug(
                        "translation_applied",
                        original=para_text[:30],
                        translated=translated[:30],
                    )
                    # Calculate font size reduction ratio
                    size_ratio = self._calculate_font_size_ratio(para_text, translated)

                    # Put all translated text in first run, clear others
                    if paragraph.runs:
                        first_run = paragraph.runs[0]
                        first_run.text = translated
                        first_run.font.name = target_font

                        # Reset character spacing to normal (Korean often has condensed spacing)
                        _reset_character_spacing(first_run)

                        # Reduce font size if needed
                        if size_ratio < 1.0 and first_run.font.size:
                            try:
                                original_size = first_run.font.size
                                new_size = int(original_size * size_ratio)
                                first_run.font.size = Pt(new_size / 12700)  # Convert EMUs to Pt
                                logger.debug(
                                    "font_size_reduced",
                                    original_size=original_size,
                                    new_size=new_size,
                                    ratio=size_ratio,
                                )
                            except Exception as e:
                                logger.debug("font_size_reduction_failed", error=str(e))

                        # Clear remaining runs
                        for run in paragraph.runs[1:]:
                            run.text = ""
                            run.font.name = target_font
                            _reset_character_spacing(run)

                        # Track successful application
                        applied_tracker[para_text] = True
                elif para_text:
                    # Log texts that weren't found in translations
                    logger.debug(
                        "translation_not_found",
                        text=para_text[:50],
                        shape_id=getattr(shape, 'shape_id', 'unknown'),
                    )
                    applied_tracker[para_text] = False

        # Handle tables - apply at cell/paragraph level
        if hasattr(shape, 'has_table') and shape.has_table:
            try:
                for row in shape.table.rows:
                    for cell in row.cells:
                        for para in cell.text_frame.paragraphs:
                            para_text = para.text.strip()
                            if para_text in translations:
                                translated = translations[para_text]
                                size_ratio = self._calculate_font_size_ratio(para_text, translated)

                                if para.runs:
                                    first_run = para.runs[0]
                                    first_run.text = translated
                                    first_run.font.name = target_font

                                    # Reset character spacing to normal
                                    _reset_character_spacing(first_run)

                                    # Reduce font size if needed
                                    if size_ratio < 1.0 and first_run.font.size:
                                        try:
                                            original_size = first_run.font.size
                                            new_size = int(original_size * size_ratio)
                                            first_run.font.size = Pt(new_size / 12700)
                                        except Exception:
                                            pass

                                    for run in para.runs[1:]:
                                        run.text = ""
                                        run.font.name = target_font
                                        _reset_character_spacing(run)
                                    applied_tracker[para_text] = True
            except Exception as e:
                logger.debug("table_apply_error", error=str(e))

    def apply_translations(
        self,
        translations: dict[str, str],
        output_path: str
    ) -> tuple[str, dict[str, bool]]:
        """
        Apply translations to the presentation.

        Args:
            translations: Dict mapping original text to translated text
            output_path: Path to save the translated presentation

        Returns:
            Tuple of (output_path, applied_tracker dict showing which texts were applied)
        """
        applied_tracker: dict[str, bool] = {}

        for slide in self.presentation.slides:
            for shape in slide.shapes:
                self._apply_to_shape(shape, translations, applied_tracker)

        self.presentation.save(output_path)

        # Log application summary
        applied_count = sum(1 for v in applied_tracker.values() if v)
        failed_count = sum(1 for v in applied_tracker.values() if not v)
        logger.info(
            "translations_applied",
            total=len(applied_tracker),
            applied=applied_count,
            failed=failed_count,
        )

        return output_path, applied_tracker
