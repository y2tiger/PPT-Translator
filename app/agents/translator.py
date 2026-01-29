import openai
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.models.schemas import Language, LANGUAGE_NAMES

logger = structlog.get_logger(__name__)

# Refusal phrases to detect when GPT refuses to translate
REFUSAL_PHRASES = [
    "przykro mi",  # Polish: I'm sorry
    "nie mogę",    # Polish: I can't
    "sorry",
    "i cannot",
    "i can't",
    "unable to",
    "cannot translate",
    "no text provided",
    "no content",
    "please provide",
]


class TranslatorAgent:
    """Agent responsible for translating text using OpenAI API."""

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
            temperature=0.3,  # Lower temperature for more consistent translations
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content

    def _is_refusal(self, response: str) -> bool:
        """Check if the response is a refusal message."""
        response_lower = response.lower()
        return any(phrase in response_lower for phrase in REFUSAL_PHRASES)

    def _should_skip_translation(self, text: str) -> bool:
        """Check if text should be skipped (numbers only, too short, etc.)."""
        stripped = text.strip()
        # Skip if only numbers, punctuation, or very short
        if stripped.isdigit():
            return True
        if len(stripped) <= 1:
            return True
        # Skip if only special characters
        if all(not c.isalnum() for c in stripped):
            return True
        return False

    async def translate(
        self,
        text: str,
        source_lang: Language,
        target_lang: Language,
        context: str | None = None,
        previous_feedback: list[str] | None = None,
    ) -> str:
        """
        Translate text from source language to target language.
        """
        # Skip translation for certain texts
        if self._should_skip_translation(text):
            logger.info("translation_skipped", reason="not_translatable", text=text[:50])
            return text

        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        logger.info(
            "translation_started",
            source=source_lang.value,
            target=target_lang.value,
            text_length=len(text),
        )

        system_prompt = f"""You are a translator. Translate {source_name} to {target_name}.

CRITICAL RULES:
1. Output ONLY the translated text - no explanations, no apologies, no comments
2. If the input looks like a brand name, company name, or acronym (e.g., "SL", "IBM"), keep it as-is
3. Translate everything else accurately
4. Maintain the same length/brevity as the original when possible
5. Never refuse - always provide a translation or return the original text unchanged

Example:
Input: "안녕하세요"
Output: "Dzień dobry"

Input: "SL"
Output: "SL"

Input: "제품 품질"
Output: "Jakość produktu" """

        user_prompt = f"{text}"

        try:
            result = await self._call_api(system_prompt, user_prompt)
            result = result.strip()

            # If GPT refused or returned something much longer, use original
            if self._is_refusal(result):
                logger.warning("translation_refusal_detected", text=text[:50], response=result[:100])
                return text

            # If response is way longer than original (3x+), something went wrong
            if len(result) > len(text) * 3 and len(text) > 5:
                logger.warning("translation_too_long", original_len=len(text), result_len=len(result))
                return text

            logger.info("translation_completed", result_length=len(result))
            return result
        except Exception as e:
            logger.error("translation_failed", error=str(e))
            return text  # Return original on error instead of raising
