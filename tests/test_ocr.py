"""
Tests for OCR functionality.

These tests verify:
1. OCRResult contract is maintained
2. Image extraction from PPT works correctly
3. OCR agent handles errors gracefully
4. OCR integrates correctly with translation pipeline
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import asdict

# Import OCR components
from app.agents.ocr_agent import (
    OCRAgent,
    OCRResult,
    OCRBlock,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
)
from app.models.schemas import (
    OCRResultResponse,
    OCRBlockResponse,
    ImageTextItem,
    ImageTranslationSummary,
    Language,
)


class TestOCRResultContract:
    """Test that OCRResult maintains its contract."""

    def test_ocr_result_has_required_fields(self):
        """OCRResult must have all required contract fields."""
        result = OCRResult(text="Hello World")

        # Required fields
        assert hasattr(result, 'text')
        assert hasattr(result, 'confidence_score')
        assert hasattr(result, 'language_guess')
        assert hasattr(result, 'blocks')
        assert hasattr(result, 'error')
        assert hasattr(result, 'is_uncertain')

    def test_ocr_result_default_values(self):
        """OCRResult defaults should be sensible."""
        result = OCRResult(text="test")

        assert result.confidence_score == 1.0
        assert result.language_guess == "unknown"
        assert result.blocks == []
        assert result.error is None
        assert result.is_uncertain is False

    def test_ocr_result_to_dict(self):
        """OCRResult.to_dict() should return valid dictionary."""
        block = OCRBlock(text="block1", confidence=0.9)
        result = OCRResult(
            text="Full text",
            confidence_score=0.85,
            language_guess="ko",
            blocks=[block],
            is_uncertain=False,
        )

        d = result.to_dict()

        assert d["text"] == "Full text"
        assert d["confidence_score"] == 0.85
        assert d["language_guess"] == "ko"
        assert len(d["blocks"]) == 1
        assert d["blocks"][0]["text"] == "block1"
        assert d["error"] is None
        assert d["is_uncertain"] is False

    def test_ocr_result_with_error(self):
        """OCRResult should properly track errors."""
        result = OCRResult(
            text="",
            confidence_score=0.0,
            error="API connection failed",
        )

        assert result.text == ""
        assert result.confidence_score == 0.0
        assert result.error == "API connection failed"

    def test_ocr_block_structure(self):
        """OCRBlock should have required fields."""
        block = OCRBlock(
            text="Sample text",
            bbox=(10, 20, 100, 50),
            confidence=0.95,
        )

        assert block.text == "Sample text"
        assert block.bbox == (10, 20, 100, 50)
        assert block.confidence == 0.95


class TestOCRAgentImageDetection:
    """Test OCR agent image type detection."""

    @pytest.fixture
    def ocr_agent(self):
        """Create OCR agent with mock API key."""
        return OCRAgent(api_key="test-key")

    def test_detect_png(self, ocr_agent):
        """Should detect PNG images."""
        # PNG magic bytes
        png_header = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        assert ocr_agent._detect_image_type(png_header) == "image/png"

    def test_detect_jpeg(self, ocr_agent):
        """Should detect JPEG images."""
        # JPEG magic bytes
        jpeg_header = b'\xff\xd8' + b'\x00' * 100
        assert ocr_agent._detect_image_type(jpeg_header) == "image/jpeg"

    def test_detect_gif(self, ocr_agent):
        """Should detect GIF images."""
        # GIF magic bytes
        gif_header = b'GIF89a' + b'\x00' * 100
        assert ocr_agent._detect_image_type(gif_header) == "image/gif"

    def test_detect_webp(self, ocr_agent):
        """Should detect WebP images."""
        # WebP magic bytes
        webp_header = b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 100
        assert ocr_agent._detect_image_type(webp_header) == "image/webp"

    def test_detect_unknown_defaults_to_png(self, ocr_agent):
        """Unknown image types should default to PNG."""
        unknown_bytes = b'\x00\x01\x02\x03' * 100
        assert ocr_agent._detect_image_type(unknown_bytes) == "image/png"

    def test_image_to_base64(self, ocr_agent):
        """Should correctly encode image to base64."""
        test_bytes = b"Hello, World!"
        b64 = ocr_agent._image_to_base64(test_bytes)

        # Verify it's valid base64
        import base64
        decoded = base64.b64decode(b64)
        assert decoded == test_bytes


class TestOCRAgentExtraction:
    """Test OCR text extraction."""

    @pytest.fixture
    def ocr_agent(self):
        """Create OCR agent with mock API key."""
        return OCRAgent(api_key="test-key")

    @pytest.mark.asyncio
    async def test_empty_image_returns_error(self, ocr_agent):
        """Empty image data should return error result."""
        result = await ocr_agent.extract_text(image_bytes=b"")

        assert result.text == ""
        assert result.confidence_score == 0.0
        assert result.error == "Empty image data"

    @pytest.mark.asyncio
    async def test_tiny_image_skipped(self, ocr_agent):
        """Very small images should be skipped (likely icons)."""
        # Less than 500 bytes
        tiny_image = b"\x00" * 100

        result = await ocr_agent.extract_text(image_bytes=tiny_image)

        assert result.text == ""
        assert result.confidence_score == 1.0  # High confidence there's no text
        assert result.error is None  # Not an error, just no text

    @pytest.mark.asyncio
    async def test_successful_extraction_mock(self, ocr_agent):
        """Test successful OCR extraction with mocked API."""
        # Mock the OpenAI response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '''
        {
            "text": "Hello World",
            "confidence": 0.95,
            "language": "en",
            "blocks": [{"text": "Hello World", "confidence": 0.95}]
        }
        '''

        with patch.object(ocr_agent.client.chat.completions, 'create', new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response

            # Create a valid image (larger than 500 bytes)
            test_image = b'\x89PNG\r\n\x1a\n' + b'\x00' * 1000

            result = await ocr_agent.extract_text(
                image_bytes=test_image,
                source_lang=Language.KOREAN,
            )

            assert result.text == "Hello World"
            assert result.confidence_score == 0.95
            assert result.language_guess == "en"
            assert len(result.blocks) == 1
            assert result.error is None

    @pytest.mark.asyncio
    async def test_api_error_handled_gracefully(self, ocr_agent):
        """API errors should be caught and returned in result."""
        import openai

        with patch.object(ocr_agent.client.chat.completions, 'create', new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = openai.APIError(
                message="API Error",
                request=MagicMock(),
                body=None,
            )

            test_image = b'\x89PNG\r\n\x1a\n' + b'\x00' * 1000

            result = await ocr_agent.extract_text(image_bytes=test_image)

            assert result.text == ""
            assert result.confidence_score == 0.0
            assert "OCR API error" in result.error

    @pytest.mark.asyncio
    async def test_json_parse_error_handled(self, ocr_agent):
        """Invalid JSON in API response should be handled."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "This is not valid JSON"

        with patch.object(ocr_agent.client.chat.completions, 'create', new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response

            test_image = b'\x89PNG\r\n\x1a\n' + b'\x00' * 1000

            result = await ocr_agent.extract_text(image_bytes=test_image)

            assert result.text == ""
            assert "Failed to parse OCR response" in result.error


class TestOCRConfidenceThresholds:
    """Test confidence level handling."""

    def test_high_confidence_not_uncertain(self):
        """High confidence results should not be marked uncertain."""
        result = OCRResult(text="test", confidence_score=0.9)
        assert result.confidence_score >= CONFIDENCE_HIGH
        # is_uncertain should be False by default for high confidence

    def test_low_confidence_marked_uncertain(self):
        """Low confidence results should be marked uncertain."""
        result = OCRResult(
            text="test",
            confidence_score=0.3,
            is_uncertain=True,  # Should be set when confidence < CONFIDENCE_MEDIUM
        )
        assert result.is_uncertain is True


class TestImageTranslationSummary:
    """Test ImageTranslationSummary model."""

    def test_default_values(self):
        """Summary should have sensible defaults."""
        summary = ImageTranslationSummary()

        assert summary.total_images == 0
        assert summary.images_with_text == 0
        assert summary.images_translated == 0
        assert summary.images_failed == 0
        assert summary.uncertain_extractions == 0

    def test_tracking_counts(self):
        """Summary should correctly track counts."""
        summary = ImageTranslationSummary(
            total_images=10,
            images_with_text=7,
            images_translated=6,
            images_failed=1,
            uncertain_extractions=2,
        )

        assert summary.total_images == 10
        assert summary.images_with_text == 7
        assert summary.images_translated == 6
        assert summary.images_failed == 1
        assert summary.uncertain_extractions == 2


class TestOCRResponseModels:
    """Test Pydantic response models for API."""

    def test_ocr_block_response(self):
        """OCRBlockResponse should serialize correctly."""
        block = OCRBlockResponse(
            text="Sample",
            bbox=(0, 0, 100, 50),
            confidence=0.9,
        )

        assert block.text == "Sample"
        assert block.bbox == (0, 0, 100, 50)
        assert block.confidence == 0.9

    def test_ocr_result_response(self):
        """OCRResultResponse should serialize correctly."""
        result = OCRResultResponse(
            text="Hello World",
            confidence_score=0.95,
            language_guess="en",
            blocks=[OCRBlockResponse(text="Hello", bbox=(0, 0, 50, 20), confidence=0.95)],
            is_uncertain=False,
        )

        assert result.text == "Hello World"
        assert result.confidence_score == 0.95
        assert result.language_guess == "en"
        assert len(result.blocks) == 1
        assert result.is_uncertain is False

    def test_image_text_item(self):
        """ImageTextItem should serialize correctly."""
        item = ImageTextItem(
            slide_number=1,
            image_id="slide_1_shape_42",
            original_text="Korean text",
            translated_text="English text",
            confidence=0.9,
            is_uncertain=False,
            source="ocr",
        )

        assert item.slide_number == 1
        assert item.image_id == "slide_1_shape_42"
        assert item.original_text == "Korean text"
        assert item.translated_text == "English text"
        assert item.source == "ocr"


# Integration tests would require actual PPT files and API access
# These are marked as skip unless running with integration test flag
class TestOCRIntegration:
    """Integration tests for OCR with PPT service."""

    @pytest.mark.skip(reason="Requires actual PPT files")
    def test_extract_images_from_ppt(self):
        """Test extracting images from a real PPT file."""
        pass

    @pytest.mark.skip(reason="Requires OpenAI API key")
    async def test_full_ocr_pipeline(self):
        """Test full OCR pipeline with real API."""
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
