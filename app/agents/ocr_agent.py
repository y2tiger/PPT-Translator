"""
OCR Agent - Extracts text from images using GPT-4o Vision API.

This agent uses the same GPT-4o Vision API already used for Visual QA,
ensuring no new dependencies are needed.

Contract:
- Input: Image bytes (JPG/PNG) or base64 string
- Output: OCRResult with extracted text, confidence, and optional blocks
"""

import base64
import json
import structlog
from dataclasses import dataclass, field
from typing import Optional
import openai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.models.schemas import Language, LANGUAGE_NAMES

logger = structlog.get_logger(__name__)


# Confidence thresholds
CONFIDENCE_HIGH = 0.8
CONFIDENCE_MEDIUM = 0.5
CONFIDENCE_LOW = 0.3


@dataclass
class OCRBlock:
    """A block of text extracted from an image with positional info."""
    text: str
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # x1, y1, x2, y2
    confidence: float = 1.0


@dataclass
class OCRResult:
    """Result of OCR extraction from an image.

    This is the contract between OCR and Translation agents.
    Any change to this contract requires updating all consumers.
    """
    text: str                           # Full extracted text
    confidence_score: float = 1.0       # Overall confidence (0.0-1.0)
    language_guess: str = "unknown"     # Detected language code
    blocks: list[OCRBlock] = field(default_factory=list)  # Detailed blocks
    error: Optional[str] = None         # Error message if extraction failed
    is_uncertain: bool = False          # True if confidence < CONFIDENCE_MEDIUM

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "text": self.text,
            "confidence_score": self.confidence_score,
            "language_guess": self.language_guess,
            "blocks": [{"text": b.text, "bbox": b.bbox, "confidence": b.confidence} for b in self.blocks],
            "error": self.error,
            "is_uncertain": self.is_uncertain,
        }


class OCRAgent:
    """Agent for extracting text from images using GPT-4o Vision API.

    Uses the same approach as VisualQAAgent for consistency:
    - GPT-4o model with vision capabilities
    - Base64 image encoding
    - Retry logic for rate limits
    """

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    def _image_to_base64(self, image_bytes: bytes) -> str:
        """Convert image bytes to base64 string."""
        return base64.b64encode(image_bytes).decode("utf-8")

    def _detect_image_type(self, image_bytes: bytes) -> str:
        """Detect image MIME type from bytes."""
        # Check magic bytes
        if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        elif image_bytes[:2] == b'\xff\xd8':
            return "image/jpeg"
        elif image_bytes[:4] == b'GIF8':
            return "image/gif"
        elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
            return "image/webp"
        else:
            # Default to PNG (most common in PPT)
            return "image/png"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIConnectionError)
        ),
    )
    async def extract_text(
        self,
        image_bytes: bytes,
        source_lang: Optional[Language] = None,
        context: str = "",
    ) -> OCRResult:
        """
        Extract text from an image using GPT-4o Vision.

        Args:
            image_bytes: Raw image bytes (PNG, JPEG, GIF, WEBP)
            source_lang: Expected source language (helps improve accuracy)
            context: Additional context about the image (e.g., "diagram", "screenshot")

        Returns:
            OCRResult with extracted text and confidence score
        """
        if not image_bytes:
            return OCRResult(
                text="",
                confidence_score=0.0,
                error="Empty image data",
            )

        # Check minimum image size (skip very small images that likely have no text)
        if len(image_bytes) < 500:  # Less than 500 bytes is likely a tiny icon
            logger.debug("ocr_skipped_tiny_image", size=len(image_bytes))
            return OCRResult(
                text="",
                confidence_score=1.0,  # High confidence there's no text
            )

        try:
            image_b64 = self._image_to_base64(image_bytes)
            mime_type = self._detect_image_type(image_bytes)

            # Build prompt based on context
            lang_hint = ""
            if source_lang:
                lang_name = LANGUAGE_NAMES.get(source_lang, str(source_lang))
                lang_hint = f"The text is expected to be in {lang_name}. "

            context_hint = ""
            if context:
                context_hint = f"This image is from: {context}. "

            prompt = f"""Extract ALL text visible in this image. {lang_hint}{context_hint}

IMPORTANT RULES:
1. Extract text EXACTLY as it appears (preserve spelling, capitalization)
2. Maintain reading order (top to bottom, left to right for most languages)
3. Preserve line breaks where they appear in the image
4. Include ALL text, even if partially visible or small
5. Do NOT translate - extract the original text only
6. If no text is visible, return empty string for text field

Return ONLY valid JSON in this exact format:
{{
  "text": "The complete extracted text here",
  "confidence": 0.95,
  "language": "ko",
  "blocks": [
    {{
      "text": "First block of text",
      "confidence": 0.9
    }}
  ]
}}

Where:
- text: All extracted text combined (separated by newlines if multi-line)
- confidence: Your confidence in extraction accuracy (0.0-1.0)
- language: Detected language code (ko, en, ja, zh, etc.)
- blocks: Optional list of text blocks if multiple distinct areas detected"""

            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=2000,
                timeout=60.0,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_b64}",
                                    "detail": "high"  # High detail for text extraction
                                }
                            }
                        ]
                    }
                ]
            )

            result_text = response.choices[0].message.content.strip()

            # Extract JSON from response (may be wrapped in ```json blocks)
            if "```" in result_text:
                import re
                json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', result_text)
                if json_match:
                    result_text = json_match.group(1)

            result = json.loads(result_text)

            # Parse response
            extracted_text = result.get("text", "").strip()
            confidence = float(result.get("confidence", 0.8))
            language = result.get("language", "unknown")

            # Parse blocks if present
            blocks = []
            for block_data in result.get("blocks", []):
                blocks.append(OCRBlock(
                    text=block_data.get("text", ""),
                    confidence=float(block_data.get("confidence", confidence)),
                ))

            # Mark as uncertain if confidence is low
            is_uncertain = confidence < CONFIDENCE_MEDIUM

            logger.info(
                "ocr_extraction_complete",
                text_length=len(extracted_text),
                confidence=round(confidence, 2),
                language=language,
                blocks_count=len(blocks),
                is_uncertain=is_uncertain,
            )

            return OCRResult(
                text=extracted_text,
                confidence_score=confidence,
                language_guess=language,
                blocks=blocks,
                is_uncertain=is_uncertain,
            )

        except json.JSONDecodeError as e:
            logger.error("ocr_json_parse_error", error=str(e))
            return OCRResult(
                text="",
                confidence_score=0.0,
                error=f"Failed to parse OCR response: {str(e)}",
            )
        except openai.APIError as e:
            logger.error("ocr_api_error", error=str(e))
            return OCRResult(
                text="",
                confidence_score=0.0,
                error=f"OCR API error: {str(e)}",
            )
        except Exception as e:
            logger.error("ocr_unexpected_error", error=str(e))
            return OCRResult(
                text="",
                confidence_score=0.0,
                error=f"Unexpected OCR error: {str(e)}",
            )

    async def extract_from_multiple_images(
        self,
        images: list[tuple[str, bytes]],  # List of (image_id, image_bytes)
        source_lang: Optional[Language] = None,
        context: str = "",
    ) -> dict[str, OCRResult]:
        """
        Extract text from multiple images.

        Args:
            images: List of (image_id, image_bytes) tuples
            source_lang: Expected source language
            context: Context about the images

        Returns:
            Dictionary mapping image_id to OCRResult
        """
        results = {}

        for image_id, image_bytes in images:
            try:
                result = await self.extract_text(
                    image_bytes=image_bytes,
                    source_lang=source_lang,
                    context=context,
                )
                results[image_id] = result

                logger.debug(
                    "ocr_image_processed",
                    image_id=image_id,
                    has_text=bool(result.text),
                    confidence=result.confidence_score,
                )
            except Exception as e:
                logger.error("ocr_image_failed", image_id=image_id, error=str(e))
                results[image_id] = OCRResult(
                    text="",
                    confidence_score=0.0,
                    error=str(e),
                )

        # Summary log
        total_with_text = sum(1 for r in results.values() if r.text)
        total_errors = sum(1 for r in results.values() if r.error)

        logger.info(
            "ocr_batch_complete",
            total_images=len(images),
            images_with_text=total_with_text,
            images_with_errors=total_errors,
        )

        return results
