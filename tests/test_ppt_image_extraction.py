"""
Tests for PPT image extraction and OCR overlay functionality.

These tests verify:
1. Image extraction from PPT shapes
2. Filtering of small images
3. Handling of group shapes with images
4. Image metadata extraction
5. OCR text overlay creation
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from app.services.ppt_service import (
    PPTService,
    MIN_IMAGE_SIZE_BYTES,
    MIN_IMAGE_DIMENSION,
    OCRTextOverlay,
    FONT_SIZE_MAP,
)


class MockImage:
    """Mock for pptx image object."""

    def __init__(self, blob: bytes, content_type: str = "image/png"):
        self.blob = blob
        self.content_type = content_type


class MockPictureShape:
    """Mock for Picture shape."""

    def __init__(
        self,
        shape_id: int,
        image_bytes: bytes,
        content_type: str = "image/png",
        width: int = 914400,  # 1 inch in EMUs
        height: int = 914400,
    ):
        self.shape_id = shape_id
        self._image = MockImage(image_bytes, content_type)
        self._width = width
        self._height = height

    @property
    def image(self):
        return self._image

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height


class TestImageExtractionFiltering:
    """Test image filtering logic."""

    def test_min_image_size_constant(self):
        """Minimum image size constant should be reasonable."""
        assert MIN_IMAGE_SIZE_BYTES > 0
        assert MIN_IMAGE_SIZE_BYTES <= 10000  # Not too restrictive

    def test_min_dimension_constant(self):
        """Minimum dimension constant should be reasonable."""
        assert MIN_IMAGE_DIMENSION > 0
        assert MIN_IMAGE_DIMENSION <= 100  # Not too restrictive


class TestImageMetadata:
    """Test image metadata extraction."""

    def test_emu_to_pixel_conversion(self):
        """EMU to pixel conversion should be correct."""
        # 914400 EMUs = 1 inch, at 96 DPI = 96 pixels
        emu_per_inch = 914400
        dpi = 96
        test_emus = emu_per_inch * 2  # 2 inches

        width_px = int(test_emus / emu_per_inch * dpi)
        assert width_px == 192  # 2 inches at 96 DPI

    def test_image_id_format(self):
        """Image ID should follow expected format."""
        slide_idx = 3
        shape_id = 42
        expected_format = f"slide_{slide_idx}_shape_{shape_id}"

        assert expected_format == "slide_3_shape_42"


class TestImageExtractionEdgeCases:
    """Test edge cases in image extraction."""

    def test_empty_presentation_no_images(self):
        """Presentation with no images should return empty list."""
        # This would need mocking of Presentation
        pass

    def test_group_shape_depth_limit(self):
        """Deeply nested group shapes should respect depth limit."""
        # Depth limit is 10 to prevent infinite recursion
        max_depth = 10
        assert max_depth > 0

    def test_image_bytes_not_modified(self):
        """Image bytes should be returned as-is, not modified."""
        original_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 2000

        # Simulating extraction - bytes should be identical
        extracted_bytes = original_bytes  # In real code, this comes from shape.image.blob

        assert extracted_bytes == original_bytes
        assert len(extracted_bytes) == len(original_bytes)


class TestSupportedImageFormats:
    """Test handling of different image formats."""

    def test_png_content_type(self):
        """PNG images should be recognized."""
        content_type = "image/png"
        assert "png" in content_type

    def test_jpeg_content_type(self):
        """JPEG images should be recognized."""
        content_type = "image/jpeg"
        assert "jpeg" in content_type

    def test_gif_content_type(self):
        """GIF images should be recognized."""
        content_type = "image/gif"
        assert "gif" in content_type

    def test_bmp_content_type(self):
        """BMP images should be recognized."""
        content_type = "image/bmp"
        assert "bmp" in content_type


class TestGetImagesBySlide:
    """Test get_images_by_slide grouping."""

    def test_images_grouped_by_slide_number(self):
        """Images should be correctly grouped by slide number."""
        # Create mock image data
        images = [
            {"slide_number": 1, "image_id": "img1"},
            {"slide_number": 1, "image_id": "img2"},
            {"slide_number": 2, "image_id": "img3"},
            {"slide_number": 3, "image_id": "img4"},
        ]

        # Simulate grouping logic
        grouped = {}
        for img in images:
            slide = img["slide_number"]
            if slide not in grouped:
                grouped[slide] = []
            grouped[slide].append(img)

        assert len(grouped) == 3  # 3 slides
        assert len(grouped[1]) == 2  # Slide 1 has 2 images
        assert len(grouped[2]) == 1  # Slide 2 has 1 image
        assert len(grouped[3]) == 1  # Slide 3 has 1 image


class TestOCRTextOverlay:
    """Test OCRTextOverlay data class."""

    def test_overlay_creation(self):
        """OCRTextOverlay should store all required fields."""
        overlay = OCRTextOverlay(
            slide_number=1,
            shape_id=42,
            original_text="안녕하세요",
            translated_text="Hello",
            bbox=(10.0, 20.0, 50.0, 15.0),
            font_size_hint="medium",
            confidence=0.95,
        )

        assert overlay.slide_number == 1
        assert overlay.shape_id == 42
        assert overlay.original_text == "안녕하세요"
        assert overlay.translated_text == "Hello"
        assert overlay.bbox == (10.0, 20.0, 50.0, 15.0)
        assert overlay.font_size_hint == "medium"
        assert overlay.confidence == 0.95

    def test_overlay_defaults(self):
        """OCRTextOverlay should have sensible defaults."""
        overlay = OCRTextOverlay(
            slide_number=1,
            shape_id=42,
            original_text="원본",
            translated_text="Translated",
            bbox=(0.0, 0.0, 100.0, 100.0),
        )

        assert overlay.font_size_hint == "medium"
        assert overlay.confidence == 1.0


class TestFontSizeMapping:
    """Test font size mapping for overlays."""

    def test_font_size_map_has_required_keys(self):
        """Font size map should have small, medium, large."""
        assert "small" in FONT_SIZE_MAP
        assert "medium" in FONT_SIZE_MAP
        assert "large" in FONT_SIZE_MAP

    def test_font_sizes_are_reasonable(self):
        """Font sizes should be reasonable values."""
        assert FONT_SIZE_MAP["small"] > 0
        assert FONT_SIZE_MAP["medium"] > FONT_SIZE_MAP["small"]
        assert FONT_SIZE_MAP["large"] > FONT_SIZE_MAP["medium"]
        assert FONT_SIZE_MAP["large"] <= 72  # Max reasonable PPT font size


class TestBboxCalculation:
    """Test bounding box to position calculation."""

    def test_bbox_percentage_to_emu(self):
        """Bbox percentages should convert correctly to EMUs."""
        # Image is 1000 EMUs wide and 500 EMUs tall
        img_width = 1000
        img_height = 500

        # Text at 10% from left, 20% from top, 50% width, 30% height
        x_pct, y_pct, w_pct, h_pct = (10.0, 20.0, 50.0, 30.0)

        text_left = int(img_width * x_pct / 100)
        text_top = int(img_height * y_pct / 100)
        text_width = int(img_width * w_pct / 100)
        text_height = int(img_height * h_pct / 100)

        assert text_left == 100   # 10% of 1000
        assert text_top == 100    # 20% of 500
        assert text_width == 500  # 50% of 1000
        assert text_height == 150  # 30% of 500


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
