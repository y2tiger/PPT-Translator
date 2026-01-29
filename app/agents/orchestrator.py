import asyncio
from typing import Optional, Callable
from app.agents.translator import TranslatorAgent
from app.agents.reviewer import ReviewerAgent
from app.models.schemas import Language, TranslationResult, ReviewFeedback


class TranslationOrchestrator:
    """
    Orchestrates the multi-agent translation process.

    The process:
    1. Translator agent produces initial translation
    2. Reviewer agent evaluates the translation
    3. If not approved, translator refines based on feedback
    4. Repeat for minimum review loops (default: 5)
    5. Final review to approve or flag for manual review
    """

    def __init__(self, api_key: str, min_review_loops: int = 5):
        self.translator = TranslatorAgent(api_key)
        self.reviewer = ReviewerAgent(api_key)
        self.min_review_loops = min_review_loops
        self.max_review_loops = 10  # Safety limit

    async def translate_with_review(
        self,
        text: str,
        source_lang: Language,
        target_lang: Language,
        context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> TranslationResult:
        """
        Translate text with iterative review process.

        Args:
            text: Text to translate
            source_lang: Source language
            target_lang: Target language
            context: Additional context for translation
            progress_callback: Callback function(iteration, message)
        """
        review_history: list[ReviewFeedback] = []
        feedback_for_translator: list[str] = []
        current_translation = ""

        # Initial translation
        if progress_callback:
            progress_callback(0, "Starting initial translation...")

        current_translation = await self.translator.translate(
            text=text,
            source_lang=source_lang,
            target_lang=target_lang,
            context=context,
        )

        # Review loop
        for iteration in range(1, self.max_review_loops + 1):
            if progress_callback:
                progress_callback(iteration, f"Review iteration {iteration}...")

            # Review current translation
            review = await self.reviewer.review(
                original_text=text,
                translated_text=current_translation,
                source_lang=source_lang,
                target_lang=target_lang,
                context=context,
                iteration=iteration,
            )
            review_history.append(review)

            # Check if we've met minimum loops and translation is approved
            if iteration >= self.min_review_loops and review.approved:
                if progress_callback:
                    progress_callback(
                        iteration, f"Translation approved after {iteration} iterations!"
                    )
                break

            # If not approved and we haven't hit max, refine translation
            if iteration < self.max_review_loops:
                # Collect feedback for translator
                feedback_for_translator = review.issues + review.suggestions

                if progress_callback:
                    progress_callback(
                        iteration,
                        f"Refining translation based on {len(feedback_for_translator)} feedback items...",
                    )

                # Get refined translation
                current_translation = await self.translator.translate(
                    text=text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    context=context,
                    previous_feedback=feedback_for_translator,
                )

        # Final review
        if progress_callback:
            progress_callback(len(review_history), "Performing final review...")

        final_review = await self.reviewer.final_review(
            original_text=text,
            translated_text=current_translation,
            source_lang=source_lang,
            target_lang=target_lang,
            review_history=review_history,
        )

        return TranslationResult(
            original=text,
            translated=current_translation,
            review_count=len(review_history),
            review_feedback=[
                f"Round {i+1} (Score: {r.score}): {', '.join(r.issues[:2]) if r.issues else 'No issues'}"
                for i, r in enumerate(review_history)
            ],
            final_score=final_review.score,
        )

    async def translate_batch(
        self,
        texts: list[str],
        source_lang: Language,
        target_lang: Language,
        context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> list[TranslationResult]:
        """
        Translate multiple texts with review process.

        For efficiency, shorter texts are batched together for initial translation,
        but each still goes through individual review.
        """
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
