from pptx import Presentation
from pptx.shapes.group import GroupShape
from typing import Generator


class PPTService:
    """Service for handling PowerPoint file operations."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.presentation = Presentation(file_path)

    def _extract_from_shape(self, shape, slide_idx: int) -> Generator[dict, None, None]:
        """Extract text from a single shape at paragraph level for better context."""
        # Handle group shapes recursively
        if isinstance(shape, GroupShape):
            for child_shape in shape.shapes:
                yield from self._extract_from_shape(child_shape, slide_idx)
            return

        # Handle text frames - extract at PARAGRAPH level (not run level)
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                # Get full paragraph text (combines all runs)
                para_text = paragraph.text.strip()
                if para_text:
                    yield {
                        "slide_number": slide_idx,
                        "shape_id": shape.shape_id,
                        "text": para_text,
                    }

        # Handle tables - extract at cell level
        if shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    if cell_text:
                        yield {
                            "slide_number": slide_idx,
                            "shape_id": shape.shape_id,
                            "is_table": True,
                            "text": cell_text,
                        }

    def extract_texts(self) -> Generator[dict, None, None]:
        """Extract all text elements from the presentation."""
        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            for shape in slide.shapes:
                yield from self._extract_from_shape(shape, slide_idx)

    def get_all_texts(self) -> list[dict]:
        """Get all texts as a list."""
        return list(self.extract_texts())

    def get_texts_by_slide(self) -> dict[int, list[str]]:
        """Get texts grouped by slide number for context-aware translation."""
        texts_by_slide: dict[int, list[str]] = {}

        for item in self.extract_texts():
            slide_num = item["slide_number"]
            text = item["text"]

            if slide_num not in texts_by_slide:
                texts_by_slide[slide_num] = []

            # Avoid duplicates within the same slide
            if text not in texts_by_slide[slide_num]:
                texts_by_slide[slide_num].append(text)

        return texts_by_slide

    def get_slide_count(self) -> int:
        """Return the number of slides."""
        return len(self.presentation.slides)

    def _apply_to_shape(self, shape, translations: dict[str, str], target_font: str = "Arial"):
        """Apply translations to a single shape at paragraph level."""
        # Handle group shapes recursively
        if isinstance(shape, GroupShape):
            for child_shape in shape.shapes:
                self._apply_to_shape(child_shape, translations, target_font)
            return

        # Handle text frames - apply at PARAGRAPH level
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                para_text = paragraph.text.strip()
                if para_text in translations:
                    translated = translations[para_text]
                    # Put all translated text in first run, clear others
                    if paragraph.runs:
                        paragraph.runs[0].text = translated
                        paragraph.runs[0].font.name = target_font
                        # Clear remaining runs
                        for run in paragraph.runs[1:]:
                            run.text = ""
                            run.font.name = target_font

        # Handle tables - apply at cell/paragraph level
        if shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    if cell_text in translations:
                        for para in cell.text_frame.paragraphs:
                            para_text = para.text.strip()
                            if para_text in translations:
                                if para.runs:
                                    para.runs[0].text = translations[para_text]
                                    para.runs[0].font.name = target_font
                                    for run in para.runs[1:]:
                                        run.text = ""
                                        run.font.name = target_font

    def apply_translations(self, translations: dict[str, str], output_path: str) -> str:
        """
        Apply translations to the presentation.

        Args:
            translations: Dict mapping original text to translated text
            output_path: Path to save the translated presentation
        """
        for slide in self.presentation.slides:
            for shape in slide.shapes:
                self._apply_to_shape(shape, translations)

        self.presentation.save(output_path)
        return output_path
