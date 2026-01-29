from pydantic import BaseModel
from typing import Optional
from enum import Enum


class Language(str, Enum):
    KOREAN = "ko"
    ENGLISH = "en"
    JAPANESE = "ja"
    CHINESE = "zh"
    SPANISH = "es"
    FRENCH = "fr"
    GERMAN = "de"


LANGUAGE_NAMES = {
    Language.KOREAN: "Korean",
    Language.ENGLISH: "English",
    Language.JAPANESE: "Japanese",
    Language.CHINESE: "Chinese",
    Language.SPANISH: "Spanish",
    Language.FRENCH: "French",
    Language.GERMAN: "German",
}


class TranslationRequest(BaseModel):
    source_language: Language
    target_language: Language
    min_review_loops: int = 5


class SlideText(BaseModel):
    slide_number: int
    shape_id: int
    original_text: str
    translated_text: Optional[str] = None


class TranslationResult(BaseModel):
    original: str
    translated: str
    review_count: int
    review_feedback: list[str]
    final_score: float


class ReviewFeedback(BaseModel):
    score: float  # 0-10
    issues: list[str]
    suggestions: list[str]
    approved: bool


class TranslationStatus(BaseModel):
    status: str
    progress: int
    total_slides: int
    current_slide: int
    review_loop: int
    message: str
