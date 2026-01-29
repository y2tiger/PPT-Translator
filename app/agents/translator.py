import openai
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.models.schemas import Language, LANGUAGE_NAMES

logger = structlog.get_logger(__name__)


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
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content

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

        Args:
            text: Text to translate
            source_lang: Source language
            target_lang: Target language
            context: Additional context for better translation
            previous_feedback: Feedback from previous review iterations
        """
        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        logger.info(
            "translation_started",
            source=source_lang.value,
            target=target_lang.value,
            text_length=len(text),
        )

        system_prompt = f"""You are an expert translator specializing in {source_name} to {target_name} translation.
Your task is to provide accurate, natural-sounding translations that preserve:
- The original meaning and nuance
- Technical terminology accuracy
- Cultural appropriateness
- Formatting and structure

You are translating content from a PowerPoint presentation, so maintain professional tone and clarity.
Only output the translated text, nothing else."""

        user_prompt = f"""Translate the following {source_name} text to {target_name}:

Original text:
{text}"""

        if context:
            user_prompt += f"""

Context from surrounding slides (for reference):
{context}"""

        if previous_feedback:
            user_prompt += f"""

Previous translation feedback to address:
{chr(10).join(f'- {fb}' for fb in previous_feedback)}

Please improve your translation based on this feedback."""

        try:
            result = await self._call_api(system_prompt, user_prompt)
            logger.info("translation_completed", result_length=len(result))
            return result.strip()
        except Exception as e:
            logger.error("translation_failed", error=str(e))
            raise
