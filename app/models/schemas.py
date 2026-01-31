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
    POLISH = "pl"


LANGUAGE_NAMES = {
    Language.KOREAN: "한국어",
    Language.ENGLISH: "English",
    Language.JAPANESE: "日本語",
    Language.CHINESE: "中文",
    Language.SPANISH: "Español",
    Language.FRENCH: "Français",
    Language.GERMAN: "Deutsch",
    Language.POLISH: "Polski",
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


class VisualIssueResponse(BaseModel):
    slide_number: int
    issue_type: str
    description: str = ""
    original_text: str = ""
    suggestion: str = ""
    severity: str = "warning"


class SlideComparisonResponse(BaseModel):
    slide_number: int
    original_image_url: str
    translated_image_url: str
    issues: list[VisualIssueResponse]
    quality_score: int
    suggestions: list[str]


class QAIterationResponse(BaseModel):
    iteration: int
    overall_score: int
    total_slides: int
    critical_issues_count: int
    texts_retranslated: int
    formatting_issues_count: int = 0  # FONT_SIZE, TRUNCATION issues
    algorithm_improvements: list[str]
    slide_comparisons: list[SlideComparisonResponse]


class QAHistoryResponse(BaseModel):
    file_id: str
    iterations: list[QAIterationResponse]
    final_score: int
