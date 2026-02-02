import io
import structlog
from pptx import Presentation
from pptx.shapes.group import GroupShape
from pptx.shapes.picture import Picture
from pptx.util import Pt, Emu
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN, MSO_AUTO_SIZE
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn
from typing import Generator, Optional
from dataclasses import dataclass

logger = structlog.get_logger(__name__)


# Minimum image size for OCR (skip tiny icons/decorations)
MIN_IMAGE_SIZE_BYTES = 1000  # 1KB minimum
MIN_IMAGE_DIMENSION = 50     # 50px minimum width/height

# Font size mapping for OCR text overlays
FONT_SIZE_MAP = {
    "small": 8,
    "medium": 12,
    "large": 18,
}


@dataclass
class OCRTextOverlay:
    """Information for creating a text overlay on an image."""
    slide_number: int
    shape_id: int                    # The image shape ID
    original_text: str               # OCR extracted text
    translated_text: str             # Translated text
    bbox: tuple[float, float, float, float]  # x%, y%, width%, height%
    font_size_hint: str = "medium"   # "small", "medium", "large"
    confidence: float = 1.0


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

    IMPORTANT: Preserves original vertical anchor (alignment).
    Only reduces left/right margins to avoid vertical position changes.
    """
    try:
        # Preserve original vertical anchor before changing margins
        original_anchor = text_frame.anchor

        # Only reduce LEFT and RIGHT margins to give horizontal space
        # Do NOT change top/bottom margins as this can affect vertical positioning
        # Margins are in EMUs (914400 EMUs = 1 inch)
        text_frame.margin_left = 27432   # ~0.03 inch
        text_frame.margin_right = 27432
        # Keep original top/bottom margins to preserve vertical position

        # Explicitly restore vertical anchor (even if it was None, try to preserve)
        # This is critical for maintaining vertical alignment
        if original_anchor is not None:
            text_frame.anchor = original_anchor

        logger.debug("margins_reduced", anchor=str(original_anchor))
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
        # Stores normalized font sizes after 2-pass calculation
        # Key: (slide_idx, shape_id, para_text) -> normalized_pt
        self._normalized_font_sizes: dict[tuple, float] = {}

    def _extract_from_shape(self, shape, slide_idx: int, depth: int = 0) -> Generator[dict, None, None]:
        """Extract text from a single shape at paragraph level for better context."""
        shape_type = str(getattr(shape, 'shape_type', 'unknown'))
        shape_id = getattr(shape, 'shape_id', 'unknown')
        shape_name = getattr(shape, 'name', '')

        # Log all shapes for debugging (helps identify missing texts)
        has_text = hasattr(shape, 'has_text_frame') and shape.has_text_frame
        has_table = hasattr(shape, 'has_table') and shape.has_table
        is_group = isinstance(shape, GroupShape)

        logger.debug(
            "shape_found",
            slide=slide_idx,
            shape_id=shape_id,
            shape_type=shape_type,
            shape_name=shape_name,
            has_text_frame=has_text,
            has_table=has_table,
            is_group=is_group,
            depth=depth,
        )

        # Handle group shapes recursively (up to depth 10 to prevent infinite loops)
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    yield from self._extract_from_shape(child_shape, slide_idx, depth + 1)
            except Exception as e:
                logger.debug("group_extraction_error", error=str(e), shape_id=shape_id)
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
                # INFO level log for production visibility - see all extracted texts
                logger.info(
                    "text_extracted",
                    slide=slide_num,
                    shape_id=item.get("shape_id"),
                    shape_type=item.get("shape_type"),
                    text=text,  # Full text for debugging
                    is_table=item.get("is_table", False),
                )

        # Log summary with all texts per slide
        total_texts = sum(len(texts) for texts in texts_by_slide.values())
        for slide_num, texts in sorted(texts_by_slide.items()):
            logger.info(
                "slide_texts_summary",
                slide=slide_num,
                text_count=len(texts),
                texts=texts,  # Show all texts for this slide
            )

        logger.info(
            "extraction_complete",
            total_slides=len(texts_by_slide),
            total_texts=total_texts,
        )

        return texts_by_slide

    def get_slide_count(self) -> int:
        """Return the number of slides."""
        return len(self.presentation.slides)

    def _extract_images_from_shape(
        self,
        shape,
        slide_idx: int,
        depth: int = 0
    ) -> Generator[dict, None, None]:
        """Extract images from a single shape, including nested group shapes.

        Returns dicts with:
        - slide_number: int
        - image_id: str (unique identifier)
        - image_bytes: bytes (raw image data)
        - content_type: str (e.g., 'image/png')
        - width: int (in EMUs)
        - height: int (in EMUs)
        """
        shape_id = getattr(shape, 'shape_id', 'unknown')

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    yield from self._extract_images_from_shape(child_shape, slide_idx, depth + 1)
            except Exception as e:
                logger.debug("group_image_extraction_error", error=str(e), shape_id=shape_id)

        # Handle Picture shapes (embedded images)
        if isinstance(shape, Picture):
            try:
                image = shape.image
                image_bytes = image.blob

                # Skip very small images (likely icons/decorations)
                if len(image_bytes) < MIN_IMAGE_SIZE_BYTES:
                    logger.debug(
                        "image_skipped_too_small",
                        slide=slide_idx,
                        shape_id=shape_id,
                        size=len(image_bytes),
                    )
                    return

                # Check image dimensions
                width = shape.width
                height = shape.height
                # EMUs to pixels (approx): 914400 EMUs = 1 inch, 96 DPI
                width_px = int(width / 914400 * 96) if width else 0
                height_px = int(height / 914400 * 96) if height else 0

                if width_px < MIN_IMAGE_DIMENSION or height_px < MIN_IMAGE_DIMENSION:
                    logger.debug(
                        "image_skipped_small_dimensions",
                        slide=slide_idx,
                        shape_id=shape_id,
                        width_px=width_px,
                        height_px=height_px,
                    )
                    return

                # Generate unique image ID
                image_id = f"slide_{slide_idx}_shape_{shape_id}"

                yield {
                    "slide_number": slide_idx,
                    "image_id": image_id,
                    "image_bytes": image_bytes,
                    "content_type": image.content_type,
                    "width": width,
                    "height": height,
                    "width_px": width_px,
                    "height_px": height_px,
                }

                logger.debug(
                    "image_extracted",
                    slide=slide_idx,
                    shape_id=shape_id,
                    content_type=image.content_type,
                    size_bytes=len(image_bytes),
                    width_px=width_px,
                    height_px=height_px,
                )

            except Exception as e:
                logger.debug("image_extraction_error", error=str(e), shape_id=shape_id)

        # Handle shapes with fill patterns that might contain images
        # (e.g., shapes filled with a picture)
        try:
            if hasattr(shape, 'fill') and shape.fill is not None:
                fill = shape.fill
                if hasattr(fill, 'type') and fill.type == MSO_SHAPE_TYPE.PICTURE:
                    # Shape is filled with a picture
                    if hasattr(fill, 'picture') and fill.picture is not None:
                        logger.debug(
                            "picture_fill_found",
                            slide=slide_idx,
                            shape_id=shape_id,
                        )
                        # Note: Extracting picture from fill is more complex
                        # and may not always be possible with python-pptx
        except Exception as e:
            logger.debug("fill_check_error", error=str(e), shape_id=shape_id)

    def extract_images(self) -> Generator[dict, None, None]:
        """Extract all images from the presentation.

        Yields dicts with image info including:
        - slide_number
        - image_id
        - image_bytes
        - content_type
        - dimensions
        """
        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            for shape in slide.shapes:
                yield from self._extract_images_from_shape(shape, slide_idx)

    def get_all_images(self) -> list[dict]:
        """Get all images as a list."""
        return list(self.extract_images())

    def get_images_by_slide(self) -> dict[int, list[dict]]:
        """Get images grouped by slide number.

        Returns dict mapping slide_number -> list of image dicts
        """
        images_by_slide: dict[int, list[dict]] = {}

        for image_info in self.extract_images():
            slide_num = image_info["slide_number"]

            if slide_num not in images_by_slide:
                images_by_slide[slide_num] = []

            images_by_slide[slide_num].append(image_info)

        # Log summary
        total_images = sum(len(imgs) for imgs in images_by_slide.values())
        for slide_num, images in sorted(images_by_slide.items()):
            logger.info(
                "slide_images_summary",
                slide=slide_num,
                image_count=len(images),
            )

        logger.info(
            "image_extraction_complete",
            total_slides_with_images=len(images_by_slide),
            total_images=total_images,
        )

        return images_by_slide

    def _find_shape_by_id(self, slide, shape_id: int, depth: int = 0):
        """Find a shape by its ID, including in group shapes."""
        for shape in slide.shapes:
            if getattr(shape, 'shape_id', None) == shape_id:
                return shape
            # Search in group shapes
            if isinstance(shape, GroupShape) and depth < 10:
                for child in shape.shapes:
                    if getattr(child, 'shape_id', None) == shape_id:
                        return child
        return None

    def apply_ocr_overlays(
        self,
        overlays: list[OCRTextOverlay],
        output_path: str,
        target_font: str = "Arial",
    ) -> tuple[str, int]:
        """
        Apply translated text overlays on top of images.

        Creates semi-transparent text boxes positioned over the original
        image locations where OCR detected text.

        Args:
            overlays: List of OCRTextOverlay objects with position and text info
            output_path: Path to save the modified presentation
            target_font: Font to use for overlay text

        Returns:
            Tuple of (output_path, number of overlays applied)
        """
        if not overlays:
            self.presentation.save(output_path)
            return output_path, 0

        applied_count = 0

        # Group overlays by slide
        overlays_by_slide: dict[int, list[OCRTextOverlay]] = {}
        for overlay in overlays:
            if overlay.slide_number not in overlays_by_slide:
                overlays_by_slide[overlay.slide_number] = []
            overlays_by_slide[overlay.slide_number].append(overlay)

        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            if slide_idx not in overlays_by_slide:
                continue

            slide_overlays = overlays_by_slide[slide_idx]

            for overlay in slide_overlays:
                try:
                    # Find the image shape
                    image_shape = self._find_shape_by_id(slide, overlay.shape_id)
                    if not image_shape:
                        logger.warning(
                            "ocr_overlay_shape_not_found",
                            slide=slide_idx,
                            shape_id=overlay.shape_id,
                        )
                        continue

                    # Get image position and size
                    img_left = image_shape.left
                    img_top = image_shape.top
                    img_width = image_shape.width
                    img_height = image_shape.height

                    # Calculate text box position from bbox percentages
                    # bbox = (x%, y%, width%, height%)
                    x_pct, y_pct, w_pct, h_pct = overlay.bbox

                    # Convert percentages to EMUs
                    text_left = img_left + int(img_width * x_pct / 100)
                    text_top = img_top + int(img_height * y_pct / 100)
                    text_width = int(img_width * w_pct / 100)
                    text_height = int(img_height * h_pct / 100)

                    # Ensure minimum size
                    min_size = Emu(Pt(20).emu)  # Minimum 20pt
                    text_width = max(text_width, min_size)
                    text_height = max(text_height, min_size)

                    # Add text box
                    textbox = slide.shapes.add_textbox(
                        text_left, text_top, text_width, text_height
                    )
                    tf = textbox.text_frame
                    tf.word_wrap = True

                    # Set text frame properties
                    tf.margin_left = Pt(2)
                    tf.margin_right = Pt(2)
                    tf.margin_top = Pt(1)
                    tf.margin_bottom = Pt(1)

                    # Add paragraph with translated text
                    p = tf.paragraphs[0]
                    p.text = overlay.translated_text
                    p.alignment = PP_ALIGN.LEFT

                    # Style the text
                    for run in p.runs:
                        run.font.name = target_font
                        font_size = FONT_SIZE_MAP.get(overlay.font_size_hint, 12)
                        run.font.size = Pt(font_size)
                        run.font.color.rgb = RGBColor(0, 0, 0)  # Black text

                    # Add semi-transparent white background to text box
                    # This makes the translated text readable over the image
                    fill = textbox.fill
                    fill.solid()
                    fill.fore_color.rgb = RGBColor(255, 255, 255)

                    # Set transparency (requires accessing XML directly)
                    # 70% opacity (30% transparent)
                    try:
                        spPr = textbox._sp.spPr
                        solidFill = spPr.find(qn('a:solidFill'))
                        if solidFill is not None:
                            srgbClr = solidFill.find(qn('a:srgbClr'))
                            if srgbClr is not None:
                                from lxml import etree
                                alpha = etree.SubElement(srgbClr, qn('a:alpha'))
                                alpha.set('val', '70000')  # 70% opacity
                    except Exception as e:
                        logger.debug("transparency_set_failed", error=str(e))

                    # Add thin border
                    line = textbox.line
                    line.color.rgb = RGBColor(100, 100, 100)
                    line.width = Pt(0.5)

                    applied_count += 1

                    logger.info(
                        "ocr_overlay_applied",
                        slide=slide_idx,
                        shape_id=overlay.shape_id,
                        text_preview=overlay.translated_text[:30],
                        bbox=overlay.bbox,
                        font_size=overlay.font_size_hint,
                    )

                except Exception as e:
                    logger.error(
                        "ocr_overlay_failed",
                        slide=slide_idx,
                        shape_id=overlay.shape_id,
                        error=str(e),
                    )

        self.presentation.save(output_path)

        logger.info(
            "ocr_overlays_complete",
            total_overlays=len(overlays),
            applied=applied_count,
        )

        return output_path, applied_count

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
            effective_width_ratio = max(width_ratio, 1.1) if force_reduction else width_ratio

            # MODERATE formula - use power of 0.7 for gentler reduction
            # For width_ratio 1.5: ~0.74 (74%)
            # For width_ratio 2.0: ~0.62 (62%)
            # For width_ratio 2.5: ~0.53 (53%)
            # For width_ratio 3.0: ~0.47 (47%)
            size_ratio = 1.0 / (effective_width_ratio ** 0.7)

            # For short original text (diagram labels in small boxes)
            # These need more reduction as the boxes are small
            if len(original) <= 5:
                # More aggressive for very short labels
                size_ratio = 1.0 / (effective_width_ratio ** 0.8)
                size_ratio = max(0.40, size_ratio)  # Allow down to 40% (not 25%)
                # For forced reduction on very short text
                if force_reduction and size_ratio > 0.7:
                    size_ratio = 0.65  # Force at least 35% reduction (not 45%)
                logger.info("ratio_short_text", orig_len=len(original), final_ratio=round(size_ratio, 3), category="<=5", forced=force_reduction)
            elif len(original) <= 10:
                # Moderate reduction for short labels
                size_ratio = 1.0 / (effective_width_ratio ** 0.75)
                size_ratio = max(0.45, size_ratio)  # Allow down to 45% (not 30%)
                if force_reduction and size_ratio > 0.75:
                    size_ratio = 0.70  # Force at least 30% reduction (not 40%)
                logger.info("ratio_short_text", orig_len=len(original), final_ratio=round(size_ratio, 3), category="<=10", forced=force_reduction)
            else:
                # Normal text - gentler reduction
                size_ratio = max(0.50, size_ratio)  # Allow down to 50% (not 40%)
                logger.info("ratio_normal_text", orig_len=len(original), final_ratio=round(size_ratio, 3), category=">10")

            return size_ratio

        logger.info("ratio_no_reduction", width_ratio=round(width_ratio, 3), reason="width_ratio_lte_1")
        return 1.0

    def _collect_font_requirements_from_shape(
        self,
        shape,
        slide_idx: int,
        translations: dict[str, str],
        requirements: list[dict],
        depth: int = 0
    ):
        """
        Pass 1: Collect font size requirements for all texts in a shape.
        Does NOT apply translations, just calculates what sizes would be needed.
        """
        shape_id = getattr(shape, 'shape_id', 'unknown')

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    self._collect_font_requirements_from_shape(
                        child_shape, slide_idx, translations, requirements, depth + 1
                    )
            except Exception as e:
                logger.debug("group_collect_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            for para_idx, paragraph in enumerate(shape.text_frame.paragraphs):
                para_text = paragraph.text.strip()
                if para_text in translations:
                    translated = translations[para_text]
                    size_ratio = self._calculate_font_size_ratio(para_text, translated)

                    # Get original font size
                    original_pt = None
                    if paragraph.runs:
                        original_size = _get_effective_font_size(paragraph.runs[0], paragraph)
                        if original_size:
                            original_pt = original_size / 12700

                    # Calculate what the new size would be
                    if original_pt and size_ratio < 1.0:
                        calculated_pt = original_pt * size_ratio
                        min_pt = 5.0 if len(translated) <= 15 else 6.0
                        calculated_pt = max(min_pt, calculated_pt)
                    elif original_pt:
                        calculated_pt = original_pt
                    else:
                        # Cannot read original font size - skip font adjustment
                        # This preserves the original font size in the PPT
                        logger.debug(
                            "font_size_unknown_skipping",
                            para_text=para_text[:30],
                            translated=translated[:30],
                        )
                        continue  # Don't add to requirements, keep original size

                    requirements.append({
                        "slide_idx": slide_idx,
                        "shape_id": shape_id,
                        "para_idx": para_idx,
                        "para_text": para_text,
                        "translated": translated,
                        "original_pt": original_pt,
                        "calculated_pt": calculated_pt,
                        "size_ratio": size_ratio,
                    })

        # Handle tables
        if hasattr(shape, 'has_table') and shape.has_table:
            try:
                for row_idx, row in enumerate(shape.table.rows):
                    for col_idx, cell in enumerate(row.cells):
                        for para_idx, para in enumerate(cell.text_frame.paragraphs):
                            para_text = para.text.strip()
                            if para_text in translations:
                                translated = translations[para_text]
                                size_ratio = self._calculate_font_size_ratio(para_text, translated)

                                original_pt = None
                                if para.runs:
                                    original_size = _get_effective_font_size(para.runs[0], para)
                                    if original_size:
                                        original_pt = original_size / 12700

                                if original_pt and size_ratio < 1.0:
                                    calculated_pt = max(6.0, original_pt * size_ratio)
                                elif original_pt:
                                    calculated_pt = original_pt
                                else:
                                    # Cannot read original font size - skip font adjustment
                                    continue

                                requirements.append({
                                    "slide_idx": slide_idx,
                                    "shape_id": f"{shape_id}_table_{row_idx}_{col_idx}",
                                    "para_idx": para_idx,
                                    "para_text": para_text,
                                    "translated": translated,
                                    "original_pt": original_pt,
                                    "calculated_pt": calculated_pt,
                                    "size_ratio": size_ratio,
                                    "is_table": True,
                                })
            except Exception as e:
                logger.debug("table_collect_error", error=str(e))

    def _normalize_font_sizes(self, requirements: list[dict]) -> dict[tuple[int, str], float]:
        """
        Pass 2: Group texts by SLIDE and original font size, normalize to group minimum.
        Returns a dict mapping (slide_idx, para_text) to normalized font size.

        IMPORTANT: Key includes slide_idx to ensure each slide is processed independently.
        Same text on different slides will have independent font size calculations.

        Grouping is done per-slide to maintain visual consistency within each slide.
        All texts with the same original font size on a slide will get the same
        calculated size (the minimum needed to fit the longest translation).

        Uses ±2pt tolerance for grouping to handle minor font size variations
        (e.g., 18.4pt and 18.6pt are grouped together as they appear visually same).
        """
        # Group by (slide_idx, original_pt_bucket) - per-slide grouping with tolerance
        # Use 2pt buckets: 0-2, 2-4, 4-6, etc. to group similar sizes
        groups: dict[tuple[int, int], list[dict]] = {}

        for req in requirements:
            slide_idx = req["slide_idx"]
            # Use 2pt bucket instead of rounding to nearest integer
            # This groups 18.4pt and 18.6pt together (both in bucket 9 = 18//2)
            # Bucket 9 covers 18.0-19.99pt
            original_pt_bucket = int(req["original_pt"] // 2)
            group_key = (slide_idx, original_pt_bucket)

            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(req)

        # For each group, find the minimum calculated_pt
        # Key is (slide_idx, para_text) to ensure slides are independent
        normalized: dict[tuple[int, str], float] = {}

        for group_key, group_items in groups.items():
            slide_idx, original_pt_bucket = group_key
            min_calculated_pt = min(item["calculated_pt"] for item in group_items)
            # Calculate the pt range this bucket covers (e.g., bucket 9 = 18-20pt)
            bucket_range_min = original_pt_bucket * 2
            bucket_range_max = bucket_range_min + 2

            logger.info(
                "font_size_group_normalized",
                slide=slide_idx,
                original_pt_range=f"{bucket_range_min}-{bucket_range_max}pt",
                item_count=len(group_items),
                min_calculated_pt=round(min_calculated_pt, 1),
                texts=[item["para_text"][:20] for item in group_items[:3]],
            )

            # Apply minimum to all items in group - KEY INCLUDES SLIDE_IDX
            for item in group_items:
                key = (item["slide_idx"], item["para_text"])
                normalized[key] = min_calculated_pt

        return normalized

    def _apply_to_shape(
        self,
        shape,
        translations: dict[str, str],
        applied_tracker: dict[str, bool],
        normalized_sizes: dict[tuple[int, str], float],
        slide_idx: int,
        target_font: str = "Arial",
        depth: int = 0
    ):
        """Apply translations to a single shape at paragraph level.

        Uses normalized_sizes dict with (slide_idx, para_text) key to ensure
        each slide's font sizes are calculated independently.
        """
        shape_type = str(getattr(shape, 'shape_type', 'unknown'))
        shape_id = getattr(shape, 'shape_id', 'unknown')

        # Handle group shapes recursively (up to depth 10)
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    self._apply_to_shape(child_shape, translations, applied_tracker, normalized_sizes, slide_idx, target_font, depth + 1)
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

            # Disable word wrap for text boxes where wrapping would cause layout issues
            # Check the longest ORIGINAL text length
            max_original_len = 0
            for para_text in all_para_texts:
                if para_text in translations:
                    max_original_len = max(max_original_len, len(para_text))

            # Disable word wrap in these cases:
            # 1. Very short original (<=6 chars) with short translation (<=20 chars)
            # 2. Medium original (<=15 chars) with similar-length translation (ratio <= 1.3)
            # 3. Translation is significantly longer (>1.5x) - prevents tiny font from wrapping
            # 4. Short original (<=20 chars) - likely a label, not paragraph text
            # This prevents mid-word breaks like "REFLEKTO R", "OBUDO WA"
            # and prevents tiny font issues like "SecuLetter Products" becoming unreadable
            should_disable_wrap = False
            if max_original_len > 0:
                if max_original_len <= 6 and max_translated_len <= 20:
                    # Very short labels always disable wrap
                    should_disable_wrap = True
                elif max_original_len <= 15 and max_translated_len <= max_original_len * 1.3:
                    # Medium labels with similar-length translation
                    should_disable_wrap = True
                elif max_original_len <= 20:
                    # Short original text (likely labels) - disable wrap to prevent tiny font
                    should_disable_wrap = True
                elif max_translated_len > max_original_len * 1.5:
                    # Translation is much longer - disable wrap to prevent excessive shrinking
                    should_disable_wrap = True

            if should_disable_wrap:
                _disable_word_wrap(shape.text_frame)
                logger.info(
                    "word_wrap_disabled_short_label",
                    shape_id=shape_id,
                    max_original_len=max_original_len,
                    max_translated_len=max_translated_len,
                )

            for paragraph in shape.text_frame.paragraphs:
                para_text = paragraph.text.strip()
                if para_text in translations:
                    translated = translations[para_text]

                    # Use pre-calculated normalized font size if available
                    # Key is (slide_idx, para_text) to ensure slides are independent
                    normalized_pt = normalized_sizes.get((slide_idx, para_text))

                    # INFO level log for production visibility
                    logger.info(
                        "applying_translation",
                        shape_id=shape_id,
                        shape_type=shape_type,
                        original=para_text[:40],
                        translated=translated[:40],
                        normalized_pt=round(normalized_pt, 1) if normalized_pt else None,
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

                        # Apply normalized font size (from 2-pass calculation)
                        if normalized_pt:
                            original_size = _get_effective_font_size(first_run, paragraph)
                            original_pt = original_size / 12700 if original_size else None

                            # Only reduce font size, never increase
                            if original_pt is None or normalized_pt < original_pt:
                                _apply_font_size(first_run, normalized_pt)
                                logger.info(
                                    "font_normalized",
                                    text=translated[:20],
                                    original_pt=round(original_pt, 1) if original_pt else None,
                                    normalized_pt=round(normalized_pt, 1),
                                )
                            else:
                                logger.info(
                                    "font_kept_original",
                                    text=translated[:20],
                                    original_pt=round(original_pt, 1) if original_pt else None,
                                    normalized_pt=round(normalized_pt, 1),
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
                        logger.debug(
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
                            if normalized_pt:
                                _apply_font_size(run, normalized_pt)
                            applied_tracker[para_text] = True
                            logger.info("run_added_successfully", text=translated[:20], normalized_pt=normalized_pt)
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
                                # Key is (slide_idx, para_text) for slide-independent lookup
                                normalized_pt = normalized_sizes.get((slide_idx, para_text))

                                if para.runs:
                                    first_run = para.runs[0]
                                    first_run.text = translated
                                    first_run.font.name = target_font

                                    # Reset character spacing to normal
                                    _reset_character_spacing(first_run)

                                    # Apply normalized font size
                                    if normalized_pt:
                                        original_size = _get_effective_font_size(first_run, para)
                                        original_pt = original_size / 12700 if original_size else None
                                        if original_pt is None or normalized_pt < original_pt:
                                            _apply_font_size(first_run, normalized_pt)

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
        Apply translations to the presentation using 2-pass font normalization.

        Pass 1: Collect font size requirements for all texts
        Pass 2: Group by slide and original size, normalize to group minimum

        Texts with the same original font size on a slide are grouped together
        and given the same calculated size for visual consistency.
        Visual QA can later detect and fix individual sizing issues if needed.

        Args:
            translations: Dict mapping original text to translated text
            output_path: Path to save the translated presentation

        Returns:
            Tuple of (output_path, applied_tracker dict showing which texts were applied)
        """
        applied_tracker: dict[str, bool] = {}

        # === PASS 1: Collect font size requirements ===
        logger.info("font_normalization_pass1_start", translation_count=len(translations))
        requirements: list[dict] = []

        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            for shape in slide.shapes:
                self._collect_font_requirements_from_shape(
                    shape, slide_idx, translations, requirements
                )

        logger.info("font_normalization_pass1_complete", requirements_count=len(requirements))

        # === PASS 2: Normalize font sizes by original size group ===
        normalized_sizes = self._normalize_font_sizes(requirements)
        logger.info("font_normalization_pass2_complete", normalized_count=len(normalized_sizes))

        # === PASS 3: Apply translations with normalized font sizes ===
        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            for shape in slide.shapes:
                self._apply_to_shape(shape, translations, applied_tracker, normalized_sizes, slide_idx)

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

    def apply_adjustments(
        self,
        adjustments: list[dict],
        output_path: str
    ) -> tuple[str, int]:
        """
        Apply format adjustments (alignment, font size) based on Visual QA feedback.

        Args:
            adjustments: List of adjustment dicts with keys:
                - slide_number: int
                - text: str (the translated text to find)
                - adjustment_type: "alignment" or "font_size"
                - target_value: For alignment: "top", "middle", "bottom"
                                For font_size: "increase", "decrease"
            output_path: Path to save the adjusted presentation

        Returns:
            Tuple of (output_path, number of adjustments applied)
        """
        if not adjustments:
            return output_path, 0

        # Categorize adjustments by type
        alignment_adjustments: list[dict] = []
        word_wrap_adjustments: list[dict] = []
        margin_adjustments: list[dict] = []
        horizontal_align_adjustments: list[dict] = []
        auto_fit_adjustments: list[dict] = []
        font_size_adjustments: list[dict] = []  # Individual text box adjustments

        for adj in adjustments:
            adj_type = adj.get("adjustment_type", "")
            slide_num = adj.get("slide_number", 0)

            if adj_type == "alignment":
                alignment_adjustments.append(adj)
            elif adj_type == "word_wrap":
                word_wrap_adjustments.append(adj)
            elif adj_type == "margin":
                margin_adjustments.append(adj)
            elif adj_type == "horizontal_align":
                horizontal_align_adjustments.append(adj)
            elif adj_type == "auto_fit":
                auto_fit_adjustments.append(adj)
            elif adj_type == "font_size":
                # Apply to individual text boxes, not whole slide
                font_size_adjustments.append(adj)
            elif adj_type == "overflow":
                # Overflow: apply font_size decrease to specific text
                font_size_adjustments.append({
                    **adj,
                    "adjustment_type": "font_size",
                    "target_value": "decrease"
                })
            elif adj_type == "text_box":
                # Text box width issues: apply font_size decrease to specific text
                font_size_adjustments.append({
                    **adj,
                    "adjustment_type": "font_size",
                    "target_value": "decrease"
                })

        # Group adjustments by slide
        alignments_by_slide: dict[int, list[dict]] = {}
        for adj in alignment_adjustments:
            slide_num = adj.get("slide_number", 0)
            if slide_num not in alignments_by_slide:
                alignments_by_slide[slide_num] = []
            alignments_by_slide[slide_num].append(adj)

        word_wrap_by_slide: dict[int, list[dict]] = {}
        for adj in word_wrap_adjustments:
            slide_num = adj.get("slide_number", 0)
            if slide_num not in word_wrap_by_slide:
                word_wrap_by_slide[slide_num] = []
            word_wrap_by_slide[slide_num].append(adj)

        margin_by_slide: dict[int, list[dict]] = {}
        for adj in margin_adjustments:
            slide_num = adj.get("slide_number", 0)
            if slide_num not in margin_by_slide:
                margin_by_slide[slide_num] = []
            margin_by_slide[slide_num].append(adj)

        horizontal_align_by_slide: dict[int, list[dict]] = {}
        for adj in horizontal_align_adjustments:
            slide_num = adj.get("slide_number", 0)
            if slide_num not in horizontal_align_by_slide:
                horizontal_align_by_slide[slide_num] = []
            horizontal_align_by_slide[slide_num].append(adj)

        auto_fit_by_slide: dict[int, list[dict]] = {}
        for adj in auto_fit_adjustments:
            slide_num = adj.get("slide_number", 0)
            if slide_num not in auto_fit_by_slide:
                auto_fit_by_slide[slide_num] = []
            auto_fit_by_slide[slide_num].append(adj)

        font_size_by_slide: dict[int, list[dict]] = {}
        for adj in font_size_adjustments:
            slide_num = adj.get("slide_number", 0)
            if slide_num not in font_size_by_slide:
                font_size_by_slide[slide_num] = []
            font_size_by_slide[slide_num].append(adj)

        applied_count = 0

        # Map alignment string to MSO_ANCHOR enum
        alignment_map = {
            "top": MSO_ANCHOR.TOP,
            "middle": MSO_ANCHOR.MIDDLE,
            "center": MSO_ANCHOR.MIDDLE,
            "bottom": MSO_ANCHOR.BOTTOM,
        }

        # Map horizontal alignment string to PP_ALIGN enum
        horizontal_align_map = {
            "left": PP_ALIGN.LEFT,
            "center": PP_ALIGN.CENTER,
            "right": PP_ALIGN.RIGHT,
            "justify": PP_ALIGN.JUSTIFY,
        }

        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            # Apply alignment adjustments (individual text matching)
            if slide_idx in alignments_by_slide:
                slide_alignments = alignments_by_slide[slide_idx]
                for shape in slide.shapes:
                    applied_count += self._apply_alignment_to_shape(
                        shape, slide_alignments, alignment_map
                    )

            # Apply word_wrap adjustments
            if slide_idx in word_wrap_by_slide:
                slide_word_wraps = word_wrap_by_slide[slide_idx]
                for shape in slide.shapes:
                    applied_count += self._apply_word_wrap_to_shape(
                        shape, slide_word_wraps
                    )

            # Apply margin adjustments
            if slide_idx in margin_by_slide:
                slide_margins = margin_by_slide[slide_idx]
                for shape in slide.shapes:
                    applied_count += self._apply_margin_to_shape(
                        shape, slide_margins
                    )

            # Apply horizontal alignment adjustments
            if slide_idx in horizontal_align_by_slide:
                slide_h_aligns = horizontal_align_by_slide[slide_idx]
                for shape in slide.shapes:
                    applied_count += self._apply_horizontal_align_to_shape(
                        shape, slide_h_aligns, horizontal_align_map
                    )

            # Apply auto_fit adjustments
            if slide_idx in auto_fit_by_slide:
                slide_auto_fits = auto_fit_by_slide[slide_idx]
                for shape in slide.shapes:
                    applied_count += self._apply_auto_fit_to_shape(
                        shape, slide_auto_fits
                    )

            # Apply font size adjustments (individual text boxes, not whole slide)
            if slide_idx in font_size_by_slide:
                slide_font_sizes = font_size_by_slide[slide_idx]
                for shape in slide.shapes:
                    applied_count += self._apply_font_size_to_shape_individual(
                        shape, slide_font_sizes
                    )

        self.presentation.save(output_path)

        logger.info(
            "adjustments_applied",
            total_adjustments=len(adjustments),
            applied=applied_count,
        )

        return output_path, applied_count

    def _apply_font_size_to_slide_with_factor(self, slide, adjustment_factor: float) -> int:
        """Apply font size adjustment to all text boxes on a slide with a specific factor.

        Args:
            slide: The slide to adjust
            adjustment_factor: Multiplier for font sizes (e.g., 0.85 for 15% decrease)

        Returns:
            Number of adjustments made
        """
        applied = 0

        for shape in slide.shapes:
            applied += self._apply_font_size_to_shape_recursive(shape, adjustment_factor)

        return applied

    def _apply_font_size_to_slide(self, slide, direction: str) -> int:
        """Apply font size adjustment to all text boxes on a slide.

        Args:
            slide: The slide to adjust
            direction: "increase" or "decrease"

        Returns:
            Number of adjustments made
        """
        adjustment_factor = 1.2 if direction == "increase" else 0.85
        return self._apply_font_size_to_slide_with_factor(slide, adjustment_factor)

    def _apply_font_size_to_shape_recursive(self, shape, adjustment_factor: float, depth: int = 0) -> int:
        """Recursively apply font size adjustment to a shape and its children."""
        applied = 0

        # Handle group shapes
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    applied += self._apply_font_size_to_shape_recursive(
                        child_shape, adjustment_factor, depth + 1
                    )
            except Exception as e:
                logger.debug("group_font_adjustment_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            text_frame = shape.text_frame
            for paragraph in text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.font.size:
                        old_size_pt = run.font.size / 12700
                        new_size_pt = old_size_pt * adjustment_factor
                        new_size_pt = max(6.0, min(72.0, new_size_pt))
                        run.font.size = Pt(new_size_pt)
                        applied += 1

        # Handle tables
        if hasattr(shape, 'has_table') and shape.has_table:
            try:
                for row in shape.table.rows:
                    for cell in row.cells:
                        for paragraph in cell.text_frame.paragraphs:
                            for run in paragraph.runs:
                                if run.font.size:
                                    old_size_pt = run.font.size / 12700
                                    new_size_pt = old_size_pt * adjustment_factor
                                    new_size_pt = max(6.0, min(72.0, new_size_pt))
                                    run.font.size = Pt(new_size_pt)
                                    applied += 1
            except Exception as e:
                logger.debug("table_font_adjustment_error", error=str(e))

        return applied

    def _apply_alignment_to_shape(
        self,
        shape,
        adjustments: list[dict],
        alignment_map: dict,
        depth: int = 0
    ) -> int:
        """Apply alignment adjustments to a single shape and its children."""
        applied_count = 0

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    applied_count += self._apply_alignment_to_shape(
                        child_shape, adjustments, alignment_map, depth + 1
                    )
            except Exception as e:
                logger.debug("group_alignment_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            text_frame = shape.text_frame
            shape_text = " ".join(p.text.strip() for p in text_frame.paragraphs if p.text.strip())

            for adj in adjustments:
                adj_text = adj.get("text", "")
                target_value = adj.get("target_value", "")

                if adj_text and adj_text in shape_text and target_value in alignment_map:
                    try:
                        old_anchor = getattr(text_frame, 'anchor', None)
                        new_anchor = alignment_map[target_value]
                        text_frame.anchor = new_anchor
                        logger.info(
                            "alignment_adjusted",
                            text=adj_text[:30],
                            old=str(old_anchor),
                            new=target_value,
                        )
                        applied_count += 1
                    except Exception as e:
                        logger.debug("alignment_adjustment_error", text=adj_text[:30], error=str(e))

        # Handle tables
        if hasattr(shape, 'has_table') and shape.has_table:
            try:
                for row in shape.table.rows:
                    for cell in row.cells:
                        text_frame = cell.text_frame
                        cell_text = " ".join(p.text.strip() for p in text_frame.paragraphs if p.text.strip())

                        for adj in adjustments:
                            adj_text = adj.get("text", "")
                            target_value = adj.get("target_value", "")

                            if adj_text and adj_text in cell_text and target_value in alignment_map:
                                try:
                                    text_frame.anchor = alignment_map[target_value]
                                    applied_count += 1
                                except Exception:
                                    pass
            except Exception as e:
                logger.debug("table_alignment_error", error=str(e))

        return applied_count

    def _apply_word_wrap_to_shape(
        self,
        shape,
        adjustments: list[dict],
        depth: int = 0
    ) -> int:
        """Apply word_wrap adjustments to a single shape and its children."""
        applied_count = 0

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    applied_count += self._apply_word_wrap_to_shape(
                        child_shape, adjustments, depth + 1
                    )
            except Exception as e:
                logger.debug("group_word_wrap_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            text_frame = shape.text_frame
            shape_text = " ".join(p.text.strip() for p in text_frame.paragraphs if p.text.strip())

            for adj in adjustments:
                adj_text = adj.get("text", "")
                target_value = adj.get("target_value", "disable")

                if adj_text and adj_text in shape_text:
                    try:
                        if target_value == "disable":
                            text_frame.word_wrap = False
                        else:
                            text_frame.word_wrap = True
                        logger.info(
                            "word_wrap_adjusted",
                            text=adj_text[:30],
                            word_wrap=target_value,
                        )
                        applied_count += 1
                    except Exception as e:
                        logger.debug("word_wrap_adjustment_error", text=adj_text[:30], error=str(e))

        return applied_count

    def _apply_margin_to_shape(
        self,
        shape,
        adjustments: list[dict],
        depth: int = 0
    ) -> int:
        """Apply margin adjustments to a single shape and its children."""
        applied_count = 0

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    applied_count += self._apply_margin_to_shape(
                        child_shape, adjustments, depth + 1
                    )
            except Exception as e:
                logger.debug("group_margin_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            text_frame = shape.text_frame
            shape_text = " ".join(p.text.strip() for p in text_frame.paragraphs if p.text.strip())

            for adj in adjustments:
                adj_text = adj.get("text", "")
                target_value = adj.get("target_value", "reduce")

                if adj_text and adj_text in shape_text:
                    try:
                        if target_value == "reduce":
                            # Reduce margins to minimum (27432 EMUs = ~0.03 inches)
                            text_frame.margin_left = 27432
                            text_frame.margin_right = 27432
                            text_frame.margin_top = 27432
                            text_frame.margin_bottom = 27432
                        elif target_value == "expand":
                            # Expand margins (91440 EMUs = ~0.1 inches)
                            text_frame.margin_left = 91440
                            text_frame.margin_right = 91440
                            text_frame.margin_top = 91440
                            text_frame.margin_bottom = 91440
                        logger.info(
                            "margin_adjusted",
                            text=adj_text[:30],
                            margin=target_value,
                        )
                        applied_count += 1
                    except Exception as e:
                        logger.debug("margin_adjustment_error", text=adj_text[:30], error=str(e))

        return applied_count

    def _apply_horizontal_align_to_shape(
        self,
        shape,
        adjustments: list[dict],
        alignment_map: dict,
        depth: int = 0
    ) -> int:
        """Apply horizontal alignment adjustments to a single shape and its children."""
        applied_count = 0

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    applied_count += self._apply_horizontal_align_to_shape(
                        child_shape, adjustments, alignment_map, depth + 1
                    )
            except Exception as e:
                logger.debug("group_horizontal_align_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            text_frame = shape.text_frame
            shape_text = " ".join(p.text.strip() for p in text_frame.paragraphs if p.text.strip())

            for adj in adjustments:
                adj_text = adj.get("text", "")
                target_value = adj.get("target_value", "left")

                if adj_text and adj_text in shape_text and target_value in alignment_map:
                    try:
                        for paragraph in text_frame.paragraphs:
                            paragraph.alignment = alignment_map[target_value]
                        logger.info(
                            "horizontal_align_adjusted",
                            text=adj_text[:30],
                            alignment=target_value,
                        )
                        applied_count += 1
                    except Exception as e:
                        logger.debug("horizontal_align_adjustment_error", text=adj_text[:30], error=str(e))

        return applied_count

    def _apply_auto_fit_to_shape(
        self,
        shape,
        adjustments: list[dict],
        depth: int = 0
    ) -> int:
        """Apply auto_fit adjustments to a single shape and its children."""
        applied_count = 0

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    applied_count += self._apply_auto_fit_to_shape(
                        child_shape, adjustments, depth + 1
                    )
            except Exception as e:
                logger.debug("group_auto_fit_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            text_frame = shape.text_frame
            shape_text = " ".join(p.text.strip() for p in text_frame.paragraphs if p.text.strip())

            for adj in adjustments:
                adj_text = adj.get("text", "")
                target_value = adj.get("target_value", "shrink_text")

                if adj_text and adj_text in shape_text:
                    try:
                        if target_value == "shrink_text":
                            # Shrink text to fit the shape
                            text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
                        elif target_value == "resize_shape":
                            # Resize shape to fit text
                            text_frame.auto_size = MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT
                        elif target_value == "none":
                            # No auto-fit
                            text_frame.auto_size = MSO_AUTO_SIZE.NONE
                        logger.info(
                            "auto_fit_adjusted",
                            text=adj_text[:30],
                            auto_fit=target_value,
                        )
                        applied_count += 1
                    except Exception as e:
                        logger.debug("auto_fit_adjustment_error", text=adj_text[:30], error=str(e))

        return applied_count

    def _apply_font_size_to_shape_individual(
        self,
        shape,
        adjustments: list[dict],
        depth: int = 0
    ) -> int:
        """Apply font_size adjustments to individual matching text boxes only.

        Unlike the slide-level adjustment, this only changes font size for
        text boxes that match the adjustment's target text.
        """
        applied_count = 0

        # Handle group shapes recursively
        if isinstance(shape, GroupShape) and depth < 10:
            try:
                for child_shape in shape.shapes:
                    applied_count += self._apply_font_size_to_shape_individual(
                        child_shape, adjustments, depth + 1
                    )
            except Exception as e:
                logger.debug("group_font_size_error", error=str(e))

        # Handle text frames
        if hasattr(shape, 'has_text_frame') and shape.has_text_frame:
            text_frame = shape.text_frame
            shape_text = " ".join(p.text.strip() for p in text_frame.paragraphs if p.text.strip())

            for adj in adjustments:
                adj_text = adj.get("text", "")
                target_value = adj.get("target_value", "decrease")

                if adj_text and adj_text in shape_text:
                    try:
                        # Determine adjustment factor
                        if target_value == "decrease":
                            factor = 0.85  # 15% decrease
                        elif target_value == "increase":
                            factor = 1.15  # 15% increase
                        else:
                            # Try to parse percentage like "80%"
                            try:
                                factor = float(target_value.replace("%", "")) / 100
                            except ValueError:
                                factor = 0.85  # default to decrease

                        # Apply to all runs in this text frame
                        for paragraph in text_frame.paragraphs:
                            for run in paragraph.runs:
                                if run.font.size is not None:
                                    current_size = run.font.size.pt
                                    new_size = max(6, current_size * factor)  # Min 6pt
                                    run.font.size = Pt(new_size)

                        logger.info(
                            "font_size_individual_adjusted",
                            text=adj_text[:30],
                            direction=target_value,
                            factor=round(factor, 2),
                        )
                        applied_count += 1
                    except Exception as e:
                        logger.debug("font_size_individual_error", text=adj_text[:30], error=str(e))

        return applied_count
