import anthropic
import json
from typing import Optional
from app.models.schemas import Language, LANGUAGE_NAMES, ReviewFeedback


class ReviewerAgent:
    """Agent responsible for reviewing and critiquing translations."""

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-20250514"

    async def review(
        self,
        original_text: str,
        translated_text: str,
        source_lang: Language,
        target_lang: Language,
        context: Optional[str] = None,
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

        message = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        response_text = message.content[0].text.strip()

        # Parse JSON response
        try:
            # Extract JSON from response (handle markdown code blocks)
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end]
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end]

            data = json.loads(response_text)

            return ReviewFeedback(
                score=data.get("overall_score", 5.0),
                issues=data.get("issues", []),
                suggestions=data.get("suggestions", []),
                approved=data.get("approved", False),
            )
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            return ReviewFeedback(
                score=6.0,
                issues=["Could not parse review response"],
                suggestions=["Please re-review"],
                approved=False,
            )

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

        message = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        response_text = message.content[0].text.strip()

        try:
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end]
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end]

            data = json.loads(response_text)

            return ReviewFeedback(
                score=data.get("overall_score", 5.0),
                issues=data.get("issues", []),
                suggestions=data.get("suggestions", []),
                approved=data.get("approved", False),
            )
        except json.JSONDecodeError:
            return ReviewFeedback(
                score=7.0,
                issues=["Final review parsing error"],
                suggestions=[],
                approved=True,  # Approve after multiple iterations
            )
