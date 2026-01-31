import structlog
from pptx import Presentation
from pptx.shapes.group import GroupShape
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Pt
from typing import Generator

logger = structlog.get_logger(__name__)


def _reset_character_spacing(run):
    """
    Reset character spacing to normal (0) for a run.
    Korean text often uses condensed spacing which doesn't work well with Latin characters.
    The 'spc' attribute is in hundredths of a point (e.g., -800 = -8pt condensed).
    """
    try:
        # Access the underlying XML element for run properties
        rPr = run._r.get_or_add_rPr()
        # The 'spc' attribute is directly on rPr without namespace prefix
        # Remove existing spc attribute if present
        if 'spc' in rPr.attrib:
            del rPr.attrib['spc']
        # Set to 0 (normal spacing) - no namespace prefix needed
        rPr.set('spc', '0')
        logger.debug("character_spacing_reset", text_preview=run.text[:20] if run.text else "")
    except Exception as e:
        logger.debug("reset_spacing_failed", error=str(e))


def _apply_aggressive_autofit(text_frame):
    """
    Apply aggressive auto-fit settings to a text frame.
    This makes the text shrink to fit within the shape boundaries.
    """
    try:
        # Set auto-size to shrink text to fit the shape
        text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE

        # Reduce margins to give more space for text
        # Margins are in EMUs (914400 EMUs = 1 inch)
        # Setting to ~0.05 inch margins
        text_frame.margin_left = 45720   # ~0.05 inch
        text_frame.margin_right = 45720
        text_frame.margin_top = 45720
        text_frame.margin_bottom = 45720

        # Enable word wrap to prevent text overflow
        text_frame.word_wrap = True

        logger.debug("autofit_applied")
    except Exception as e:
        logger.debug("autofit_failed", error=str(e))


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
        Polish translations are often 2-6x longer than Korean originals.

        Key insight: Korean characters are wider than Latin characters,
        so even if the character count ratio is 3:1, the visual width ratio
        is often closer to 1.5:1. We need to account for this.
        """
        if not original or not translated:
            return 1.0

        # Count Korean characters in original
        korean_char_count = sum(1 for c in original if '\uAC00' <= c <= '\uD7A3' or '\u1100' <= c <= '\u11FF')

        # Calculate effective width ratio
        # Korean characters are roughly 1.8x wider than Latin characters
        # So "광학" (2 chars) ≈ "LENS" (4 chars) in visual width
        korean_width_factor = 1.8
        original_effective_width = (korean_char_count * korean_width_factor) + (len(original) - korean_char_count)
        translated_effective_width = len(translated)  # Latin chars are narrower

        # Calculate width ratio based on effective widths
        if original_effective_width > 0:
            width_ratio = translated_effective_width / original_effective_width
        else:
            width_ratio = len(translated) / max(len(original), 1)

        # If translated text is visually wider, reduce font size
        if width_ratio > 1.0:
            # More aggressive formula using power of 0.6 instead of 0.5
            # For width_ratio 1.5: ~0.74 (74%)
            # For width_ratio 2.0: ~0.66 (66%)
            # For width_ratio 2.5: ~0.59 (59%)
            # For width_ratio 3.0: ~0.54 (54%)
            size_ratio = 1.0 / (width_ratio ** 0.6)

            # For short original text (diagram labels), be MORE aggressive
            # These are typically in small constrained boxes
            if len(original) < 8:
                # Even more aggressive for short labels
                size_ratio = 1.0 / (width_ratio ** 0.7)
                # Allow down to 40% for diagram labels
                size_ratio = max(0.40, size_ratio)
            else:
                # Normal text: cap at 45% minimum
                size_ratio = max(0.45, size_ratio)

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
            # Apply aggressive auto-fit for text to shrink if needed
            _apply_aggressive_autofit(shape.text_frame)

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
                        # Apply aggressive auto-fit to table cells
                        _apply_aggressive_autofit(cell.text_frame)

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
