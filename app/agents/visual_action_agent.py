"""
VisualActionAgent - LLM-based visual action decision system.

This agent looks at original and translated slide images and decides
specific actions to take to improve the translated slide's visual quality.

Unlike rule-based approaches, this agent uses GPT-4o Vision to make
intelligent decisions about what adjustments are needed.
"""

import base64
import json
import structlog
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import openai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = structlog.get_logger(__name__)


@dataclass
class DetailedAction:
    """A detailed action to apply to the PPT, decided by LLM."""
    action_type: str  # "font_size", "alignment", "horizontal_align", "word_wrap", "margin", "auto_fit", "retranslate"
    target_text: str  # The text to find and modify
    slide_number: int
    parameters: dict = field(default_factory=dict)  # Action-specific parameters
    reasoning: str = ""  # LLM's explanation for why this action is needed
    confidence: float = 0.8  # Confidence score (0-1)
    priority: int = 1  # Execution priority (lower = higher priority)


@dataclass
class ActionDecisionResult:
    """Result of the action decision process."""
    slide_number: int
    actions: list[DetailedAction] = field(default_factory=list)
    quality_score: int = 0  # 0-100
    overall_assessment: str = ""


# Available actions and their parameters
AVAILABLE_ACTIONS = """
Available Actions (choose the most appropriate for each issue):

1. font_size - Change font size
   Parameters:
   - direction: "decrease" (make smaller) or "increase" (make larger)
   - percentage: optional, e.g., "80%" for 80% of original

2. alignment - Change vertical text alignment within container
   Parameters:
   - target: "top", "middle", or "bottom"

3. horizontal_align - Change horizontal text alignment
   Parameters:
   - target: "left", "center", or "right"

4. word_wrap - Enable or disable text wrapping
   Parameters:
   - enabled: "true" or "false"

5. margin - Adjust text box internal margins
   Parameters:
   - action: "reduce" (minimize margins) or "expand"

6. auto_fit - Enable text auto-fitting
   Parameters:
   - mode: "shrink_text" (shrink to fit), "resize_shape", or "none"

7. retranslate - Request a new translation
   Parameters:
   - instruction: e.g., "shorter", "more concise", "maximum 20 characters"
"""


class VisualActionAgent:
    """
    Agent that uses GPT-4o Vision to decide what actions to take
    to improve translated slide quality.
    """

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    def _encode_image(self, image_path: Path) -> str:
        """Encode image to base64."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((openai.APITimeoutError, openai.RateLimitError))
    )
    async def decide_actions(
        self,
        original_image: Path,
        translated_image: Path,
        slide_number: int,
        source_lang: str = "Korean",
        target_lang: str = "English",
    ) -> ActionDecisionResult:
        """
        Analyze original and translated slide images and decide specific actions.

        This is the core LLM-based decision making function.
        """
        if not original_image.exists() or not translated_image.exists():
            logger.warning(
                "action_decision_skipped_missing_images",
                slide=slide_number,
                original_exists=original_image.exists(),
                translated_exists=translated_image.exists(),
            )
            return ActionDecisionResult(slide_number=slide_number)

        original_b64 = self._encode_image(original_image)
        translated_b64 = self._encode_image(translated_image)

        prompt = f"""You are a PPT layout expert. Compare these two presentation slides:
- Image 1 (LEFT): Original slide in {source_lang}
- Image 2 (RIGHT): Translated slide in {target_lang}

Your task is to identify visual quality issues in the translated slide and decide
SPECIFIC ACTIONS to fix them. Focus on:

1. **Text overflow/truncation**: Text cut off or extending beyond containers
2. **Font size issues**: Text too small to read or disproportionate to original
3. **Alignment problems**: Text not aligned properly (vertically or horizontally)
4. **Layout issues**: Text boxes that look cramped, wrapped incorrectly, or mispositioned
5. **Spacing issues**: Text with too little or too much space around it

{AVAILABLE_ACTIONS}

For each issue you find, decide the BEST action to fix it.
Be specific about which text needs adjustment.

IMPORTANT:
- Only suggest actions for real problems you can see
- Prioritize the most impactful fixes
- Use the exact text visible in the slides for target_text
- If the translated slide looks good, return an empty actions list

Return as JSON:
{{
  "quality_score": 85,  // 0-100, how good does the translated slide look?
  "overall_assessment": "Brief description of the main issues",
  "actions": [
    {{
      "action_type": "font_size",
      "target_text": "The visible text to modify",
      "parameters": {{"direction": "decrease"}},
      "reasoning": "Why this action is needed",
      "confidence": 0.9,
      "priority": 1
    }},
    {{
      "action_type": "word_wrap",
      "target_text": "Another text",
      "parameters": {{"enabled": "false"}},
      "reasoning": "Text box is shrinking due to word wrap",
      "confidence": 0.85,
      "priority": 2
    }}
  ]
}}"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=2000,
                timeout=120.0,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{original_b64}",
                                    "detail": "high"
                                }
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{translated_b64}",
                                    "detail": "high"
                                }
                            }
                        ]
                    }
                ]
            )

            result_text = response.choices[0].message.content
            # Extract JSON from response
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]

            result = json.loads(result_text)

            # Parse actions
            actions = []
            for action_data in result.get("actions", []):
                if action_data.get("action_type") and action_data.get("target_text"):
                    actions.append(DetailedAction(
                        action_type=action_data.get("action_type", ""),
                        target_text=action_data.get("target_text", ""),
                        slide_number=slide_number,
                        parameters=action_data.get("parameters", {}),
                        reasoning=action_data.get("reasoning", ""),
                        confidence=action_data.get("confidence", 0.8),
                        priority=action_data.get("priority", 1),
                    ))

            logger.debug(
                "visual_action_decision_complete",
                slide=slide_number,
                quality_score=result.get("quality_score", 0),
                action_count=len(actions),
                actions=[{"type": a.action_type, "text": a.target_text[:30]} for a in actions[:5]],
            )

            return ActionDecisionResult(
                slide_number=slide_number,
                actions=actions,
                quality_score=result.get("quality_score", 50),
                overall_assessment=result.get("overall_assessment", ""),
            )

        except json.JSONDecodeError as e:
            logger.error("action_decision_json_error", slide=slide_number, error=str(e))
            return ActionDecisionResult(slide_number=slide_number)
        except Exception as e:
            logger.error("action_decision_failed", slide=slide_number, error=str(e))
            return ActionDecisionResult(slide_number=slide_number)

    def convert_to_format_adjustments(
        self,
        actions: list[DetailedAction],
        translations: dict[str, str],
    ) -> list[dict]:
        """
        Convert DetailedActions to the format expected by PPTService.apply_adjustments().

        This bridges the new LLM-based action system with the existing implementation.
        """
        from app.agents.visual_qa_agent import FormatAdjustment

        adjustments = []
        reverse_translations = {v: k for k, v in translations.items()}

        for action in actions:
            # Try to find the original text for matching
            target_text = action.target_text
            original_text = None

            # Strategy 1: Direct lookup (target_text is original)
            if target_text in translations:
                original_text = target_text
            # Strategy 2: target_text is translated, find original
            elif target_text in reverse_translations:
                original_text = reverse_translations[target_text]
            # Strategy 3: Partial match
            else:
                for orig, trans in translations.items():
                    if target_text in orig or orig in target_text:
                        original_text = orig
                        break
                    if target_text in trans or trans in target_text:
                        original_text = orig
                        break

            if not original_text:
                original_text = target_text  # Fallback

            # Convert parameters to target_value based on action type
            params = action.parameters
            target_value = ""

            if action.action_type == "font_size":
                target_value = params.get("direction", "decrease")
            elif action.action_type == "alignment":
                target_value = params.get("target", "middle")
            elif action.action_type == "horizontal_align":
                target_value = params.get("target", "left")
            elif action.action_type == "word_wrap":
                target_value = "disable" if params.get("enabled") == "false" else "enable"
            elif action.action_type == "margin":
                target_value = params.get("action", "reduce")
            elif action.action_type == "auto_fit":
                target_value = params.get("mode", "shrink_text")
            elif action.action_type == "retranslate":
                target_value = params.get("instruction", "shorter")

            adjustments.append({
                "slide_number": action.slide_number,
                "text": original_text,
                "adjustment_type": action.action_type,
                "target_value": target_value,
                "priority": action.priority,
            })

            logger.debug(
                "action_converted_to_adjustment",
                slide=action.slide_number,
                type=action.action_type,
                target_text=action.target_text[:30],
                original_text=original_text[:30] if original_text else "",
                target_value=target_value,
            )

        return adjustments


async def decide_slide_actions(
    agent: VisualActionAgent,
    original_images: list[Path],
    translated_images: list[Path],
    source_lang: str = "Korean",
    target_lang: str = "English",
) -> list[ActionDecisionResult]:
    """
    Convenience function to decide actions for multiple slides.
    """
    results = []

    for i, (orig_img, trans_img) in enumerate(zip(original_images, translated_images), 1):
        result = await agent.decide_actions(
            original_image=orig_img,
            translated_image=trans_img,
            slide_number=i,
            source_lang=source_lang,
            target_lang=target_lang,
        )
        results.append(result)

    return results
