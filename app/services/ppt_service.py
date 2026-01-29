from pptx import Presentation
from pptx.util import Pt
from typing import Generator
import copy
import os


class PPTService:
    """Service for handling PowerPoint file operations."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.presentation = Presentation(file_path)

    def extract_texts(self) -> Generator[dict, None, None]:
        """Extract all text elements from the presentation."""
        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        for run in paragraph.runs:
                            if run.text.strip():
                                yield {
                                    "slide_number": slide_idx,
                                    "shape_id": shape.shape_id,
                                    "paragraph_idx": paragraph._p.getparent().index(paragraph._p),
                                    "run_idx": paragraph._p.index(run._r),
                                    "text": run.text,
                                    "font_size": run.font.size,
                                    "font_name": run.font.name,
                                    "bold": run.font.bold,
                                    "italic": run.font.italic,
                                }

                # Handle tables
                if shape.has_table:
                    for row_idx, row in enumerate(shape.table.rows):
                        for col_idx, cell in enumerate(row.cells):
                            if cell.text.strip():
                                yield {
                                    "slide_number": slide_idx,
                                    "shape_id": shape.shape_id,
                                    "is_table": True,
                                    "row_idx": row_idx,
                                    "col_idx": col_idx,
                                    "text": cell.text,
                                }

    def get_all_texts(self) -> list[dict]:
        """Get all texts as a list."""
        return list(self.extract_texts())

    def get_slide_count(self) -> int:
        """Return the number of slides."""
        return len(self.presentation.slides)

    def apply_translations(self, translations: dict[str, str], output_path: str) -> str:
        """
        Apply translations to the presentation.

        Args:
            translations: Dict mapping original text to translated text
            output_path: Path to save the translated presentation
        """
        for slide in self.presentation.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        for run in paragraph.runs:
                            original = run.text
                            if original in translations:
                                run.text = translations[original]

                # Handle tables
                if shape.has_table:
                    for row in shape.table.rows:
                        for cell in row.cells:
                            if cell.text in translations:
                                # Preserve formatting by updating the first paragraph
                                if cell.text_frame.paragraphs:
                                    for para in cell.text_frame.paragraphs:
                                        full_text = para.text
                                        if full_text in translations:
                                            if para.runs:
                                                # Keep first run with translated text
                                                para.runs[0].text = translations[full_text]
                                                # Clear other runs
                                                for run in para.runs[1:]:
                                                    run.text = ""

        self.presentation.save(output_path)
        return output_path

    def get_context_for_slide(self, slide_number: int) -> str:
        """Get context from surrounding text for better translation."""
        texts = []
        for slide_idx, slide in enumerate(self.presentation.slides, 1):
            if abs(slide_idx - slide_number) <= 1:  # Current and adjacent slides
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        texts.append(shape.text_frame.text)
        return " ".join(texts)
