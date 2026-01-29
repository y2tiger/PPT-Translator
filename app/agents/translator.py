import json
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

    async def translate_slide_batch(
        self,
        texts: list[str],
        source_lang: Language,
        target_lang: Language,
        slide_number: int,
    ) -> dict[str, str]:
        """
        Translate all texts from a slide together for better context.
        Returns a dict mapping original text to translated text.
        """
        if not texts:
            return {}

        # Filter out texts that should be skipped
        texts_to_translate = []
        skipped_texts = {}

        for text in texts:
            if self._should_skip_translation(text):
                skipped_texts[text] = text  # Keep original
            else:
                texts_to_translate.append(text)

        if not texts_to_translate:
            return skipped_texts

        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        logger.info(
            "slide_batch_translation_started",
            slide_number=slide_number,
            text_count=len(texts_to_translate),
        )

        # Build numbered list for translation
        numbered_texts = "\n".join(
            f"[{i+1}] {text}" for i, text in enumerate(texts_to_translate)
        )

        system_prompt = f"""You are a professional translator. Translate the following texts from {source_name} to {target_name}.

These texts are all from the same presentation slide, so maintain consistency in terminology and style.

CRITICAL RULES:
1. Return ONLY a JSON object mapping the number to the translation
2. Keep brand names, company names, or acronyms (e.g., "SL", "IBM") unchanged
3. Maintain the same length/brevity as the original when possible
4. Never add explanations, apologies, or comments
5. If a text is already in the target language, return it unchanged

Example input:
[1] 안녕하세요
[2] SL 회사
[3] 제품 품질

Example output:
{{"1": "Dzień dobry", "2": "SL Firma", "3": "Jakość produktu"}}

Now translate:"""

        user_prompt = numbered_texts

        try:
            result = await self._call_api(system_prompt, user_prompt, max_tokens=4096)
            result = result.strip()

            # Extract JSON from response (handle markdown code blocks)
            if "```" in result:
                # Extract content between code blocks
                import re
                json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', result)
                if json_match:
                    result = json_match.group(1)

            # Parse JSON response
            translations_dict = json.loads(result)

            # Build result mapping
            result_mapping = dict(skipped_texts)  # Start with skipped texts

            for i, text in enumerate(texts_to_translate):
                key = str(i + 1)
                if key in translations_dict:
                    translated = translations_dict[key]
                    # Validate translation
                    if self._is_refusal(translated):
                        result_mapping[text] = text
                    elif len(translated) > len(text) * 3 and len(text) > 5:
                        result_mapping[text] = text
                    else:
                        result_mapping[text] = translated
                else:
                    result_mapping[text] = text  # Keep original if not found

            logger.info(
                "slide_batch_translation_completed",
                slide_number=slide_number,
                translated_count=len(result_mapping),
            )
            return result_mapping

        except json.JSONDecodeError as e:
            logger.error("batch_translation_json_error", error=str(e), response=result[:200])
            # Fallback: translate individually
            result_mapping = dict(skipped_texts)
            for text in texts_to_translate:
                result_mapping[text] = await self.translate(text, source_lang, target_lang)
            return result_mapping
        except Exception as e:
            logger.error("batch_translation_failed", error=str(e))
            # Return originals on error
            return {text: text for text in texts}
