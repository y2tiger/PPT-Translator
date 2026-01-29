import anthropic
import json
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.models.schemas import Language, LANGUAGE_NAMES, ReviewFeedback

logger = structlog.get_logger(__name__)


class ReviewerAgent:
    """Agent responsible for reviewing and critiquing translations."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (anthropic.RateLimitError, anthropic.APIConnectionError)
        ),
    )
    async def _call_api(
        self, system_prompt: str, user_prompt: str, max_tokens: int = 2048
    ) -> str:
        """Make API call with retry logic."""
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return message.content[0].text

    def _parse_json_response(self, response_text: str) -> dict | None:
        """Parse JSON from response, handling markdown code blocks."""
        try:
            text = response_text.strip()
            if "```json" in text:
                json_start = text.find("```json") + 7
                json_end = text.find("```", json_start)
                text = text[json_start:json_end]
            elif "```" in text:
                json_start = text.find("```") + 3
                json_end = text.find("```", json_start)
                text = text[json_start:json_end]
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("json_parse_failed", response_preview=response_text[:200])
            return None

    async def review(
        self,
        original_text: str,
        translated_text: str,
        source_lang: Language,
        target_lang: Language,
        context: str | None = None,
        iteration: int = 1,
    ) -> ReviewFeedback:
        """
        Review a translation and provide feedback.

        Args:
            original_text: Original text
            translated_text: Translated text to review
            source_lang: Source language
            target_lang: Target language
            context: Additional context
            iteration: Current review iteration number
        """
        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        logger.info(
            "review_started",
            iteration=iteration,
            source=source_lang.value,
            target=target_lang.value,
        )

        system_prompt = f"""You are an expert translation quality reviewer specializing in {source_name} to {target_name} translations.
Your task is to critically evaluate translations for:

1. **Accuracy** (0-10): Does the translation convey the exact meaning?
2. **Fluency** (0-10): Does it read naturally in {target_name}?
3. **Terminology** (0-10): Are technical terms translated correctly?
4. **Style** (0-10): Is the tone appropriate for a presentation?
5. **Completeness** (0-10): Is all information preserved?

This is review iteration {iteration}. Be thorough and critical.
{"Be especially strict - look for subtle errors that might have been missed." if iteration > 3 else ""}

Respond in JSON format:
{{
    "scores": {{
        "accuracy": <0-10>,
        "fluency": <0-10>,
        "terminology": <0-10>,
        "style": <0-10>,
        "completeness": <0-10>
    }},
    "overall_score": <0-10>,
    "issues": ["list of specific issues found"],
    "suggestions": ["list of specific improvement suggestions"],
    "approved": <true if overall_score >= 8.5 and no critical issues, false otherwise>
}}"""

        user_prompt = f"""Review this translation:

**Original ({source_name}):**
{original_text}

**Translation ({target_name}):**
{translated_text}"""

        if context:
            user_prompt += f"""

**Context:**
{context}"""

        try:
            response_text = await self._call_api(system_prompt, user_prompt)
            data = self._parse_json_response(response_text)

            if data:
                result = ReviewFeedback(
                    score=data.get("overall_score", 5.0),
                    issues=data.get("issues", []),
                    suggestions=data.get("suggestions", []),
                    approved=data.get("approved", False),
                )
            else:
                result = ReviewFeedback(
                    score=6.0,
                    issues=["Could not parse review response"],
                    suggestions=["Please re-review"],
                    approved=False,
                )

            logger.info(
                "review_completed",
                iteration=iteration,
                score=result.score,
                approved=result.approved,
            )
            return result

        except Exception as e:
            logger.error("review_failed", iteration=iteration, error=str(e))
            raise

    async def final_review(
        self,
        original_text: str,
        translated_text: str,
        source_lang: Language,
        target_lang: Language,
        review_history: list[ReviewFeedback],
    ) -> ReviewFeedback:
        """
        Perform a final comprehensive review after multiple iterations.
        """
        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        logger.info(
            "final_review_started",
            history_length=len(review_history),
        )

        history_summary = "\n".join(
            f"Round {i+1}: Score {fb.score}/10 - Issues: {', '.join(fb.issues[:2]) if fb.issues else 'None'}"
            for i, fb in enumerate(review_history)
        )

        system_prompt = f"""You are performing the FINAL quality review for a {source_name} to {target_name} translation.
This translation has gone through {len(review_history)} review iterations.

Review history:
{history_summary}

Provide your final assessment. Be thorough but fair.
The translation should be approved if it's publication-ready.

Respond in JSON format:
{{
    "overall_score": <0-10>,
    "issues": ["any remaining issues"],
    "suggestions": ["final suggestions if any"],
    "approved": <true if ready for use, false otherwise>,
    "summary": "brief summary of translation quality"
}}"""

        user_prompt = f"""**Original ({source_name}):**
{original_text}

**Final Translation ({target_name}):**
{translated_text}"""

        try:
            response_text = await self._call_api(system_prompt, user_prompt)
            data = self._parse_json_response(response_text)

            if data:
                result = ReviewFeedback(
                    score=data.get("overall_score", 5.0),
                    issues=data.get("issues", []),
                    suggestions=data.get("suggestions", []),
                    approved=data.get("approved", False),
                )
            else:
                # On parse failure, do NOT auto-approve
                result = ReviewFeedback(
                    score=6.0,
                    issues=["Final review parsing error - manual review recommended"],
                    suggestions=[],
                    approved=False,
                )

            logger.info(
                "final_review_completed",
                score=result.score,
                approved=result.approved,
            )
            return result

        except Exception as e:
            logger.error("final_review_failed", error=str(e))
            raise
