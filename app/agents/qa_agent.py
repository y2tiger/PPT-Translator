import json
import openai
import structlog
from dataclasses import dataclass
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.models.schemas import Language, LANGUAGE_NAMES

logger = structlog.get_logger(__name__)


@dataclass
class QAIssue:
    """Represents a quality issue found in translation."""
    issue_type: str  # "untranslated", "too_long", "inconsistent"
    original_text: str
    translated_text: str
    suggestion: str
    slide_number: int = 0


@dataclass
class QAResult:
    """Result of QA analysis."""
    passed: bool
    issues: list[QAIssue]
    summary: str


class QAAgent:
    """Agent responsible for quality assurance of translations."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIConnectionError)
        ),
    )
    async def _call_api(
        self, system_prompt: str, user_prompt: str, max_tokens: int = 4096
    ) -> str:
        """Make API call with retry logic."""
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content

    def _contains_source_language(self, text: str, source_lang: Language) -> bool:
        """Check if text contains source language characters."""
        if source_lang == Language.KOREAN:
            # Check for Korean characters (Hangul)
            return any('\uAC00' <= char <= '\uD7A3' for char in text)
        elif source_lang == Language.JAPANESE:
            # Check for Japanese characters (Hiragana, Katakana, Kanji)
            return any(
                ('\u3040' <= char <= '\u309F') or  # Hiragana
                ('\u30A0' <= char <= '\u30FF') or  # Katakana
                ('\u4E00' <= char <= '\u9FFF')     # Kanji
                for char in text
            )
        elif source_lang == Language.CHINESE:
            # Check for Chinese characters
            return any('\u4E00' <= char <= '\u9FFF' for char in text)
        return False

    async def analyze_translations(
        self,
        original_texts: dict[int, list[str]],  # slide_num -> texts
        translations: dict[str, str],  # original -> translated
        source_lang: Language,
        target_lang: Language,
    ) -> QAResult:
        """
        Analyze translations for quality issues.

        Returns QAResult with list of issues found.
        """
        issues: list[QAIssue] = []

        # Check each translation
        for slide_num, texts in original_texts.items():
            for original in texts:
                translated = translations.get(original, original)

                # Issue 1: Text not translated (still contains source language)
                if self._contains_source_language(translated, source_lang):
                    # Check if original was supposed to be translated
                    if original != translated or self._contains_source_language(original, source_lang):
                        issues.append(QAIssue(
                            issue_type="untranslated",
                            original_text=original,
                            translated_text=translated,
                            suggestion=f"Text still contains {LANGUAGE_NAMES[source_lang]} characters",
                            slide_number=slide_num,
                        ))

                # Issue 2: Translation missing (same as original when it shouldn't be)
                if original == translated and self._contains_source_language(original, source_lang):
                    issues.append(QAIssue(
                        issue_type="missing",
                        original_text=original,
                        translated_text=translated,
                        suggestion="Translation not found - text unchanged",
                        slide_number=slide_num,
                    ))

        # Log results
        logger.info(
            "qa_analysis_complete",
            total_texts=sum(len(texts) for texts in original_texts.values()),
            issues_found=len(issues),
        )

        # Determine if passed
        passed = len(issues) == 0
        summary = f"Found {len(issues)} issues" if issues else "All translations verified"

        return QAResult(passed=passed, issues=issues, summary=summary)

    async def suggest_fixes(
        self,
        issues: list[QAIssue],
        source_lang: Language,
        target_lang: Language,
    ) -> dict[str, str]:
        """
        Get suggested fixes for translation issues.

        Returns dict mapping original text to suggested translation.
        """
        if not issues:
            return {}

        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        # Build prompt with issues
        issues_text = "\n".join(
            f"[{i+1}] Original: {issue.original_text}\n    Current: {issue.translated_text}\n    Issue: {issue.issue_type}"
            for i, issue in enumerate(issues)
        )

        system_prompt = f"""You are a translation quality fixer. Fix the following translation issues from {source_name} to {target_name}.

RULES:
1. Return ONLY a JSON object mapping the issue number to the corrected translation
2. Keep translations CONCISE - suitable for presentation slides
3. Keep brand names and acronyms (e.g., "SL") unchanged
4. If text has mixed languages, translate only the {source_name} parts

Example output:
{{"1": "Fixed translation", "2": "Another fix"}}

Issues to fix:"""

        try:
            result = await self._call_api(system_prompt, issues_text)
            result = result.strip()

            # Extract JSON
            if "```" in result:
                import re
                json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', result)
                if json_match:
                    result = json_match.group(1)

            fixes_dict = json.loads(result)

            # Build result mapping
            fixes = {}
            for i, issue in enumerate(issues):
                key = str(i + 1)
                if key in fixes_dict:
                    fixes[issue.original_text] = fixes_dict[key]
                    logger.info(
                        "fix_suggested",
                        original=issue.original_text[:30],
                        fix=fixes_dict[key][:30],
                    )

            return fixes

        except Exception as e:
            logger.error("fix_suggestion_failed", error=str(e))
            return {}
