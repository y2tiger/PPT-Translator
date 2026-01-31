import structlog
from pptx import Presentation
from pptx.shapes.group import GroupShape
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


def _reduce_margins(text_frame):
    """
    Reduce text frame margins to give more space for text.
    Note: We do NOT use TEXT_TO_FIT_SHAPE as PowerPoint's auto-fit
    has minimum size limits that cause text truncation.
    Instead, we manually reduce font sizes.
    """
    try:
        # Reduce margins to give more space for text
        # Margins are in EMUs (914400 EMUs = 1 inch)
        # Setting to ~0.03 inch margins (minimal)
        text_frame.margin_left = 27432   # ~0.03 inch
        text_frame.margin_right = 27432
        text_frame.margin_top = 27432
        text_frame.margin_bottom = 27432
        logger.debug("margins_reduced")
    except Exception as e:
        logger.debug("margin_reduction_failed", error=str(e))


def _disable_word_wrap(text_frame):
    """
    Disable word wrap to prevent text from breaking mid-word.
    For short labels in diagram boxes, we want text to stay on one line
    and shrink the font instead of wrapping.
    """
    try:
        text_frame.word_wrap = False
        logger.debug("word_wrap_disabled")
    except Exception as e:
        logger.debug("word_wrap_disable_failed", error=str(e))


def _get_effective_font_size(run, paragraph):
    """
    Get the effective font size for a run, checking multiple sources.
    Returns size in EMUs or None if not determinable.
    """
    # Try run's font size first
    if run.font.size is not None:
        return run.font.size

    # Try to get from paragraph's default run properties
    try:
        if hasattr(paragraph, '_defRPr') and paragraph._defRPr is not None:
            if hasattr(paragraph._defRPr, 'sz') and paragraph._defRPr.sz is not None:
                return paragraph._defRPr.sz
    except Exception:
        pass

    # Try to get from other runs in the same paragraph
    try:
        for r in paragraph.runs:
            if r.font.size is not None:
                return r.font.size
    except Exception:
        pass

    return None


def _apply_font_size(run, new_size_pt):
    """
    Apply a specific font size in points to a run.
    """
    try:
        run.font.size = Pt(new_size_pt)
        return True
    except Exception as e:
        logger.debug("font_size_apply_failed", error=str(e))
        return False


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

        IMPORTANT: PowerPoint's auto-fit has minimum size limits, so we must
        be very aggressive with manual font reduction to prevent truncation.
        """
        if not original or not translated:
            logger.info("ratio_calc_empty", original_len=len(original) if original else 0, translated_len=len(translated) if translated else 0)
            return 1.0

        # Count Korean characters in original
        korean_char_count = sum(1 for c in original if '\uAC00' <= c <= '\uD7A3' or '\u1100' <= c <= '\u11FF')

        # Calculate effective width ratio
        # Korean characters are roughly 2.0x wider than Latin characters visually
        # So "광학" (2 Korean chars) ≈ "LENS" (4 Latin chars) in visual width
        korean_width_factor = 2.0
        original_effective_width = (korean_char_count * korean_width_factor) + (len(original) - korean_char_count)
        translated_effective_width = len(translated)

        # Calculate width ratio based on effective widths
        if original_effective_width > 0:
            width_ratio = translated_effective_width / original_effective_width
        else:
            width_ratio = len(translated) / max(len(original), 1)

        # Log calculation details for debugging
        logger.info(
            "ratio_calculation",
            original=original[:30],
            translated=translated[:30],
            korean_chars=korean_char_count,
            orig_len=len(original),
            trans_len=len(translated),
            orig_eff_width=round(original_effective_width, 1),
            width_ratio=round(width_ratio, 3),
        )

        # IMPORTANT: For short Korean labels translated to Latin scripts,
        # even if width_ratio ~= 1.0, we may still need reduction because:
        # 1. Korean text boxes are sized for Korean fonts
        # 2. Latin fonts have different spacing characteristics
        # 3. PowerPoint's text boxes don't always match our calculations
        # Force reduction for short labels with mostly Korean original text
        force_reduction = False
        if korean_char_count > 0 and len(original) <= 6:
            # Short Korean labels (1-6 chars) almost always need reduction
            # even if our calculation says they're equal width
            force_reduction = True
            logger.info(
                "force_reduction_short_korean",
                original=original,
                translated=translated,
                reason="short_korean_label",
            )

        # If translated text is visually wider OR force reduction for short Korean labels
        if width_ratio > 1.0 or force_reduction:
            # Use width_ratio for calculation, but for force_reduction cases
            # where width_ratio <= 1.0, use a minimum effective ratio
            effective_width_ratio = max(width_ratio, 1.2) if force_reduction else width_ratio

            # VERY aggressive formula - use power of 0.85 for direct proportion
            # For width_ratio 1.5: ~0.70 (70%)
            # For width_ratio 2.0: ~0.55 (55%)
            # For width_ratio 2.5: ~0.46 (46%)
            # For width_ratio 3.0: ~0.39 (39%)
            size_ratio = 1.0 / (effective_width_ratio ** 0.85)

            # For short original text (diagram labels in small boxes)
            # These need EXTREME reduction as the boxes are tiny
            # With word_wrap disabled, text must fit on one line
            if len(original) <= 5:
                # Extremely aggressive for very short labels (like LENS, BEZEL)
                size_ratio = 1.0 / (effective_width_ratio ** 0.95)
                size_ratio = max(0.25, size_ratio)  # Allow down to 25%
                # For forced reduction on very short text, be even more aggressive
                if force_reduction and size_ratio > 0.6:
                    size_ratio = 0.55  # Force at least 45% reduction
                logger.info("ratio_short_text", orig_len=len(original), final_ratio=round(size_ratio, 3), category="<=5", forced=force_reduction)
            elif len(original) <= 10:
                # Very aggressive for short labels
                size_ratio = 1.0 / (effective_width_ratio ** 0.90)
                size_ratio = max(0.30, size_ratio)  # Allow down to 30%
                if force_reduction and size_ratio > 0.65:
                    size_ratio = 0.60  # Force at least 40% reduction
                logger.info("ratio_short_text", orig_len=len(original), final_ratio=round(size_ratio, 3), category="<=10", forced=force_reduction)
            else:
                # Normal text
                size_ratio = max(0.40, size_ratio)  # Allow down to 40%
                logger.info("ratio_normal_text", orig_len=len(original), final_ratio=round(size_ratio, 3), category=">10")

            return size_ratio

        logger.info("ratio_no_reduction", width_ratio=round(width_ratio, 3), reason="width_ratio_lte_1")
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
        shape_type = str(getattr(shape, 'shape_type', 'unknown'))
        shape_id = getattr(shape, 'shape_id', 'unknown')

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
            # Reduce margins to give more space (don't use TEXT_TO_FIT_SHAPE)
            _reduce_margins(shape.text_frame)

            # Check if this shape contains short text that should not wrap
            # We'll enable/disable word wrap based on the longest translated text
            all_para_texts = [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
            max_translated_len = 0
            for para_text in all_para_texts:
                if para_text in translations:
                    max_translated_len = max(max_translated_len, len(translations[para_text]))

            # Disable word wrap for short labels (prevents mid-word breaks like "LEN S")
            # For labels under 20 chars, keep on one line and shrink font instead
            if max_translated_len > 0 and max_translated_len <= 20:
                _disable_word_wrap(shape.text_frame)
                logger.info(
                    "word_wrap_disabled_short_label",
                    shape_id=shape_id,
                    max_translated_len=max_translated_len,
                )

            for paragraph in shape.text_frame.paragraphs:
                para_text = paragraph.text.strip()
                if para_text in translations:
                    translated = translations[para_text]
                    # Calculate font size reduction ratio
                    size_ratio = self._calculate_font_size_ratio(para_text, translated)

                    # INFO level log for production visibility
                    logger.info(
                        "applying_translation",
                        shape_id=shape_id,
                        shape_type=shape_type,
                        original=para_text[:40],
                        translated=translated[:40],
                        size_ratio=round(size_ratio, 3),
                        has_runs=len(paragraph.runs) > 0,
                        run_count=len(paragraph.runs),
                    )

                    # Put all translated text in first run, clear others
                    if paragraph.runs:
                        first_run = paragraph.runs[0]
                        first_run.text = translated
                        first_run.font.name = target_font

                        # Reset character spacing to normal (Korean often has condensed spacing)
                        _reset_character_spacing(first_run)

                        # Reduce font size if needed
                        if size_ratio < 1.0:
                            # Get effective font size (handles inherited/theme fonts)
                            original_size = _get_effective_font_size(first_run, paragraph)

                            if original_size:
                                # original_size is in EMUs, convert to points
                                original_pt = original_size / 12700
                                new_pt = original_pt * size_ratio
                                # Minimum readable size: 5pt for short labels, 6pt otherwise
                                # Short labels are on single line (word_wrap disabled) so can be smaller
                                min_pt = 5.0 if len(translated) <= 15 else 6.0
                                new_pt = max(min_pt, new_pt)
                                _apply_font_size(first_run, new_pt)
                                logger.info(
                                    "font_reduced",
                                    text=translated[:20],
                                    original_pt=round(original_pt, 1),
                                    new_pt=round(new_pt, 1),
                                    ratio=round(size_ratio, 2),
                                )
                            else:
                                # Font size not found - apply a reasonable default based on text length
                                # For short labels in small boxes, use smaller fonts
                                # These are likely diagram labels where text must fit on one line
                                if len(translated) <= 8:
                                    default_pt = 6.0  # Very small for tight boxes
                                elif len(translated) <= 12:
                                    default_pt = 7.0
                                elif len(translated) <= 20:
                                    default_pt = 8.0
                                else:
                                    default_pt = 9.0
                                _apply_font_size(first_run, default_pt)
                                logger.info(
                                    "font_default_applied",
                                    text=translated[:20],
                                    default_pt=default_pt,
                                    trans_len=len(translated),
                                    reason="no_original_size_found",
                                )
                        else:
                            # size_ratio >= 1.0, no reduction needed
                            logger.info(
                                "font_no_reduction",
                                text=translated[:20],
                                size_ratio=round(size_ratio, 3),
                                reason="ratio_gte_1",
                            )

                        # Clear remaining runs
                        for run in paragraph.runs[1:]:
                            run.text = ""
                            run.font.name = target_font
                            _reset_character_spacing(run)

                        # Track successful application
                        applied_tracker[para_text] = True
                    else:
                        # No runs in paragraph - need to add text directly
                        logger.warning(
                            "no_runs_in_paragraph",
                            shape_id=shape_id,
                            shape_type=shape_type,
                            text=para_text[:30],
                        )
                        # Try to add a run
                        try:
                            run = paragraph.add_run()
                            run.text = translated
                            run.font.name = target_font
                            if size_ratio < 1.0:
                                # Apply a default small font for short labels
                                if len(translated) <= 8:
                                    _apply_font_size(run, 6.0)
                                elif len(translated) <= 12:
                                    _apply_font_size(run, 7.0)
                                elif len(translated) <= 20:
                                    _apply_font_size(run, 8.0)
                                else:
                                    _apply_font_size(run, 9.0)
                            applied_tracker[para_text] = True
                            logger.info("run_added_successfully", text=translated[:20])
                        except Exception as e:
                            logger.error("add_run_failed", error=str(e))
                            applied_tracker[para_text] = False
                elif para_text:
                    # Log texts that weren't found in translations
                    logger.debug(
                        "translation_not_found",
                        text=para_text[:50],
                        shape_id=shape_id,
                    )
                    applied_tracker[para_text] = False

        # Handle tables - apply at cell/paragraph level
        if hasattr(shape, 'has_table') and shape.has_table:
            try:
                for row in shape.table.rows:
                    for cell in row.cells:
                        # Reduce margins for table cells
                        _reduce_margins(cell.text_frame)

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
                                    if size_ratio < 1.0:
                                        original_size = _get_effective_font_size(first_run, para)
                                        if original_size:
                                            original_pt = original_size / 12700
                                            new_pt = max(6.0, original_pt * size_ratio)
                                            _apply_font_size(first_run, new_pt)
                                        else:
                                            # Default for table cells
                                            default_pt = 8.0 if len(translated) <= 15 else 9.0
                                            _apply_font_size(first_run, default_pt)

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
