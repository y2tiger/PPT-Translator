import json
import openai
import structlog
from dataclasses import dataclass, field
from enum import Enum
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.models.schemas import Language, LANGUAGE_NAMES

logger = structlog.get_logger(__name__)


class IssueStage(str, Enum):
    """Stage where the issue occurred."""
    EXTRACTION = "extraction"  # Text not extracted from PPT
    TRANSLATION = "translation"  # Text extracted but not translated
    APPLICATION = "application"  # Text translated but not applied back


class IssueSeverity(str, Enum):
    """Severity of the issue."""
    CRITICAL = "critical"  # Must fix - text completely missing
    WARNING = "warning"  # Should fix - partial issue
    INFO = "info"  # Minor - cosmetic issue


@dataclass
class DiagnosticIssue:
    """Detailed diagnostic issue with root cause."""
    stage: IssueStage
    severity: IssueSeverity
    original_text: str
    current_text: str
    root_cause: str
    suggested_fix: str
    slide_number: int = 0
    shape_info: str = ""


@dataclass
class DiagnosticReport:
    """Complete diagnostic report."""
    total_original_texts: int
    total_extracted: int
    total_translated: int
    total_applied: int
    issues: list[DiagnosticIssue] = field(default_factory=list)
    extraction_rate: float = 0.0
    translation_rate: float = 0.0
    application_rate: float = 0.0
    summary: str = ""
    recommendations: list[str] = field(default_factory=list)


class DiagnosticAgent:
    """Agent for diagnosing translation pipeline issues."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    def _contains_source_language(self, text: str, source_lang: Language) -> bool:
        """Check if text contains source language characters."""
        if source_lang == Language.KOREAN:
            return any('\uAC00' <= char <= '\uD7A3' for char in text)
        elif source_lang == Language.JAPANESE:
            return any(
                ('\u3040' <= char <= '\u309F') or
                ('\u30A0' <= char <= '\u30FF') or
                ('\u4E00' <= char <= '\u9FFF')
                for char in text
            )
        elif source_lang == Language.CHINESE:
            return any('\u4E00' <= char <= '\u9FFF' for char in text)
        return False

    async def analyze_pipeline(
        self,
        extracted_texts: dict[int, list[str]],  # What was extracted from PPT
        translations: dict[str, str],  # Original -> Translated mapping
        applied_texts: dict[str, bool],  # Which texts were successfully applied
        source_lang: Language,
        target_lang: Language,
    ) -> DiagnosticReport:
        """
        Analyze the full translation pipeline to find issues.
        """
        issues: list[DiagnosticIssue] = []

        # Count totals
        all_extracted = []
        for slide_num, texts in extracted_texts.items():
            all_extracted.extend([(slide_num, t) for t in texts])

        total_extracted = len(all_extracted)
        total_translated = 0
        total_applied = 0

        for slide_num, original_text in all_extracted:
            translated = translations.get(original_text)
            was_applied = applied_texts.get(original_text, False)

            # Check translation status
            if translated and translated != original_text:
                total_translated += 1

                # Check if source language still present in translation
                if self._contains_source_language(translated, source_lang):
                    issues.append(DiagnosticIssue(
                        stage=IssueStage.TRANSLATION,
                        severity=IssueSeverity.WARNING,
                        original_text=original_text,
                        current_text=translated,
                        root_cause="Translation incomplete - source language characters remain",
                        suggested_fix="Re-translate with explicit instruction to translate all text",
                        slide_number=slide_num,
                    ))
            elif translated == original_text:
                # Translation returned original - might be intentional (brand name) or failure
                if self._contains_source_language(original_text, source_lang):
                    issues.append(DiagnosticIssue(
                        stage=IssueStage.TRANSLATION,
                        severity=IssueSeverity.CRITICAL,
                        original_text=original_text,
                        current_text=original_text,
                        root_cause="Text not translated - returned unchanged",
                        suggested_fix="Force translation of this text",
                        slide_number=slide_num,
                    ))
            else:
                # No translation found at all
                issues.append(DiagnosticIssue(
                    stage=IssueStage.TRANSLATION,
                    severity=IssueSeverity.CRITICAL,
                    original_text=original_text,
                    current_text=original_text,
                    root_cause="Translation missing from response",
                    suggested_fix="Re-request translation for this text",
                    slide_number=slide_num,
                ))

            # Check application status
            if was_applied:
                total_applied += 1

        # Calculate rates
        extraction_rate = 100.0  # Assume all extracted for now
        translation_rate = (total_translated / total_extracted * 100) if total_extracted > 0 else 0
        application_rate = (total_applied / total_extracted * 100) if total_extracted > 0 else 0

        # Generate recommendations
        recommendations = []

        critical_count = sum(1 for i in issues if i.severity == IssueSeverity.CRITICAL)
        warning_count = sum(1 for i in issues if i.severity == IssueSeverity.WARNING)

        if critical_count > 0:
            recommendations.append(f"CRITICAL: {critical_count} texts need immediate attention")

        if translation_rate < 90:
            recommendations.append(f"Translation rate is {translation_rate:.1f}% - consider simplifying prompts")

        if application_rate < translation_rate:
            recommendations.append("Some translations not applied - check key matching logic")

        # Group issues by type for summary
        stage_counts = {}
        for issue in issues:
            stage_counts[issue.stage.value] = stage_counts.get(issue.stage.value, 0) + 1

        summary = f"Extracted: {total_extracted}, Translated: {total_translated}, Issues: {len(issues)}"
        if stage_counts:
            summary += f" ({', '.join(f'{k}: {v}' for k, v in stage_counts.items())})"

        report = DiagnosticReport(
            total_original_texts=total_extracted,
            total_extracted=total_extracted,
            total_translated=total_translated,
            total_applied=total_applied,
            issues=issues,
            extraction_rate=extraction_rate,
            translation_rate=translation_rate,
            application_rate=application_rate,
            summary=summary,
            recommendations=recommendations,
        )

        logger.info(
            "diagnostic_complete",
            total_extracted=total_extracted,
            total_translated=total_translated,
            issues_count=len(issues),
            translation_rate=f"{translation_rate:.1f}%",
        )

        return report

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIConnectionError)
        ),
    )
    async def get_targeted_fixes(
        self,
        issues: list[DiagnosticIssue],
        source_lang: Language,
        target_lang: Language,
    ) -> dict[str, str]:
        """
        Get targeted fixes for specific issues with context about why they failed.
        """
        if not issues:
            return {}

        # Only fix critical and warning issues
        fixable_issues = [i for i in issues if i.severity in (IssueSeverity.CRITICAL, IssueSeverity.WARNING)]

        if not fixable_issues:
            return {}

        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        # Build detailed prompt with issue context
        issues_text = "\n".join(
            f"[{i+1}] Text: \"{issue.original_text}\"\n"
            f"    Problem: {issue.root_cause}\n"
            f"    Current: \"{issue.current_text}\""
            for i, issue in enumerate(fixable_issues)
        )

        system_prompt = f"""You are a translation repair specialist. Fix these failed translations from {source_name} to {target_name}.

These translations failed for specific reasons. Provide correct translations.

RULES:
1. Return ONLY a JSON object mapping issue number to the corrected translation
2. Keep translations CONCISE - suitable for presentation slides
3. Keep brand names/acronyms unchanged (e.g., "SL")
4. MUST translate all {source_name} text - do not return original

Example output:
{{"1": "Corrected translation", "2": "Another fix"}}

Issues to fix:"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": issues_text},
                ],
            )
            result = response.choices[0].message.content.strip()

            # Extract JSON
            if "```" in result:
                import re
                json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', result)
                if json_match:
                    result = json_match.group(1)

            fixes_dict = json.loads(result)

            # Build result
            fixes = {}
            for i, issue in enumerate(fixable_issues):
                key = str(i + 1)
                if key in fixes_dict:
                    new_translation = fixes_dict[key]
                    # Verify the fix is actually different and doesn't contain source language
                    if new_translation != issue.original_text:
                        fixes[issue.original_text] = new_translation
                        logger.info(
                            "targeted_fix_applied",
                            original=issue.original_text[:30],
                            fix=new_translation[:30],
                            root_cause=issue.root_cause,
                        )

            return fixes

        except Exception as e:
            logger.error("targeted_fix_failed", error=str(e))
            return {}
