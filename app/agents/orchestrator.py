import structlog
from typing import Optional, Callable
from app.agents.translator import TranslatorAgent
from app.agents.reviewer import ReviewerAgent
from app.models.schemas import Language, TranslationResult, ReviewFeedback

logger = structlog.get_logger(__name__)


class TranslationOrchestrator:
    """
    Orchestrates the multi-agent translation process.

    The process:
    1. Translator agent produces initial translation
    2. Reviewer agent evaluates and provides feedback
    3. Return final translation result
    """

    def __init__(self, api_key: str):
        self.translator = TranslatorAgent(api_key)
        self.reviewer = ReviewerAgent(api_key)

    async def translate_with_review(
        self,
        text: str,
        source_lang: Language,
        target_lang: Language,
        context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> TranslationResult:
        """
        Translate text with single review.

        Args:
            text: Text to translate
            source_lang: Source language
            target_lang: Target language
            context: Additional context for translation
            progress_callback: Callback function(iteration, message)
        """
        # Initial translation
        if progress_callback:
            progress_callback(0, "번역 중...")

        current_translation = await self.translator.translate(
            text=text,
            source_lang=source_lang,
            target_lang=target_lang,
            context=context,
        )

        # Single review
        if progress_callback:
            progress_callback(1, "리뷰 중...")

        review = await self.reviewer.review(
            original_text=text,
            translated_text=current_translation,
            source_lang=source_lang,
            target_lang=target_lang,
            context=context,
            iteration=1,
        )

        if progress_callback:
            progress_callback(1, "완료")

        return TranslationResult(
            original=text,
            translated=current_translation,
            review_count=1,
            review_feedback=[
                f"Score: {review.score}/10 - {', '.join(review.issues[:2]) if review.issues else 'No issues'}"
            ],
            final_score=review.score,
        )

    async def translate_batch(
        self,
        texts: list[str],
        source_lang: Language,
        target_lang: Language,
        context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[TranslationResult]:
        """Translate multiple texts with review process."""
        results = []

        for idx, text in enumerate(texts):
            if progress_callback:
                progress_callback(idx + 1, len(texts), f"Translating item {idx + 1}/{len(texts)}")

            result = await self.translate_with_review(
                text=text,
                source_lang=source_lang,
                target_lang=target_lang,
                context=context,
            )
            results.append(result)

        return results
