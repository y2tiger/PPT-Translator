from pptx import Presentation
from pptx.shapes.group import GroupShape
from typing import Generator


class PPTService:
    """Service for handling PowerPoint file operations."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.presentation = Presentation(file_path)

    def _extract_from_shape(self, shape, slide_idx: int) -> Generator[dict, None, None]:
        """Extract text from a single shape, handling groups recursively."""
        # Handle group shapes recursively
        if isinstance(shape, GroupShape):
            for child_shape in shape.shapes:
                yield from self._extract_from_shape(child_shape, slide_idx)
            return

        # Handle text frames
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.text.strip():
                        yield {
                            "slide_number": slide_idx,
                            "shape_id": shape.shape_id,
                            "text": run.text,
                        }

        # Handle tables
        if shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        yield {
                            "slide_number": slide_idx,
                            "shape_id": shape.shape_id,
                            "is_table": True,
                            "text": cell.text,
                        }

    def extract_texts(self) -> Generator[dict, None, None]:
        """Extract all text elements from the presentation."""
        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            for shape in slide.shapes:
                yield from self._extract_from_shape(shape, slide_idx)

    def get_all_texts(self) -> list[dict]:
        """Get all texts as a list."""
        return list(self.extract_texts())

    def get_slide_count(self) -> int:
        """Return the number of slides."""
        return len(self.presentation.slides)

    def _apply_to_shape(self, shape, translations: dict[str, str], target_font: str = "Arial"):
        """Apply translations to a single shape, handling groups recursively."""
        # Handle group shapes recursively
        if isinstance(shape, GroupShape):
            for child_shape in shape.shapes:
                self._apply_to_shape(child_shape, translations, target_font)
            return

        # Handle text frames
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.text in translations:
                        run.text = translations[run.text]
                        # Apply global font to avoid Korean font display issues
                        run.font.name = target_font

        # Handle tables
        if shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    if cell.text in translations:
                        for para in cell.text_frame.paragraphs:
                            if para.text in translations:
                                if para.runs:
                                    para.runs[0].text = translations[para.text]
                                    para.runs[0].font.name = target_font
                                    for run in para.runs[1:]:
                                        run.text = ""

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
