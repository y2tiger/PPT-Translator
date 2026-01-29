import anthropic
from typing import Optional
from app.models.schemas import Language, LANGUAGE_NAMES


class TranslatorAgent:
    """Agent responsible for translating text using Claude API."""

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-20250514"

    async def translate(
        self,
        text: str,
        source_lang: Language,
        target_lang: Language,
        context: Optional[str] = None,
        previous_feedback: Optional[list[str]] = None,
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

        message = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        return message.content[0].text.strip()

    async def batch_translate(
        self,
        texts: list[str],
        source_lang: Language,
        target_lang: Language,
        context: Optional[str] = None,
    ) -> list[str]:
        """Translate multiple texts efficiently."""
        source_name = LANGUAGE_NAMES[source_lang]
        target_name = LANGUAGE_NAMES[target_lang]

        if not texts:
            return []

        # For efficiency, batch small texts together
        numbered_texts = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(texts))

        system_prompt = f"""You are an expert translator specializing in {source_name} to {target_name} translation.
Translate each numbered item maintaining the same numbering format.
Preserve technical terms, formatting, and professional tone.
Only output the translations with their numbers, nothing else."""

        user_prompt = f"""Translate each numbered item from {source_name} to {target_name}:

{numbered_texts}"""

        if context:
            user_prompt += f"""

Context for reference:
{context}"""

        message = self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Parse the response
        response_text = message.content[0].text.strip()
        translations = []

        for i in range(len(texts)):
            marker = f"[{i+1}]"
            next_marker = f"[{i+2}]"

            start = response_text.find(marker)
            if start != -1:
                start += len(marker)
                end = response_text.find(next_marker) if i < len(texts) - 1 else len(response_text)
                if end == -1:
                    end = len(response_text)
                translations.append(response_text[start:end].strip())
            else:
                translations.append(texts[i])  # Fallback to original

        return translations
