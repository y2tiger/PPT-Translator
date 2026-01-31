"""Font utilities for PPT processing."""
import subprocess
import structlog
from pathlib import Path
from pptx import Presentation
from pptx.util import Pt

logger = structlog.get_logger(__name__)

# Mapping of common Korean fonts to free alternatives
FONT_MAPPING = {
    # Windows Korean fonts → Free alternatives
    "맑은 고딕": "Noto Sans KR",
    "Malgun Gothic": "Noto Sans KR",
    "굴림": "Nanum Gothic",
    "Gulim": "Nanum Gothic",
    "돋움": "Nanum Gothic",
    "Dotum": "Nanum Gothic",
    "바탕": "Nanum Myeongjo",
    "Batang": "Nanum Myeongjo",
    "궁서": "Nanum Myeongjo",
    "Gungsuh": "Nanum Myeongjo",
    "새굴림": "Nanum Gothic",
    "New Gulim": "Nanum Gothic",
    # Common presentation fonts
    "Arial": "Noto Sans",
    "Calibri": "Noto Sans",
    "Times New Roman": "Noto Serif",
    "Verdana": "Noto Sans",
    # Keep these as-is (usually available)
    "Nanum Gothic": "Nanum Gothic",
    "Nanum Myeongjo": "Nanum Myeongjo",
    "Noto Sans KR": "Noto Sans KR",
    "Noto Sans CJK KR": "Noto Sans CJK KR",
}


def extract_fonts_from_ppt(ppt_path: str) -> set[str]:
    """Extract all font names used in a PPT file."""
    fonts = set()

    try:
        prs = Presentation(ppt_path)

        for slide in prs.slides:
            for shape in slide.shapes:
                fonts.update(_extract_fonts_from_shape(shape))

        logger.info("fonts_extracted", ppt_path=ppt_path, fonts=list(fonts))
        return fonts

    except Exception as e:
        logger.error("font_extraction_failed", error=str(e))
        return set()


def _extract_fonts_from_shape(shape) -> set[str]:
    """Extract fonts from a single shape (recursive for groups)."""
    fonts = set()

    # Handle grouped shapes
    if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
        for child_shape in shape.shapes:
            fonts.update(_extract_fonts_from_shape(child_shape))
        return fonts

    # Handle text frames
    if hasattr(shape, "text_frame"):
        try:
            for paragraph in shape.text_frame.paragraphs:
                # Paragraph-level font
                if paragraph.font and paragraph.font.name:
                    fonts.add(paragraph.font.name)

                # Run-level fonts
                for run in paragraph.runs:
                    if run.font and run.font.name:
                        fonts.add(run.font.name)
        except Exception:
            pass

    # Handle tables
    if hasattr(shape, "table"):
        try:
            for row in shape.table.rows:
                for cell in row.cells:
                    for paragraph in cell.text_frame.paragraphs:
                        if paragraph.font and paragraph.font.name:
                            fonts.add(paragraph.font.name)
                        for run in paragraph.runs:
                            if run.font and run.font.name:
                                fonts.add(run.font.name)
        except Exception:
            pass

    return fonts


def get_system_fonts() -> set[str]:
    """Get list of fonts available on the system."""
    try:
        result = subprocess.run(
            ["fc-list", "--format=%{family}\n"],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            fonts = set()
            for line in result.stdout.strip().split("\n"):
                # fc-list may return comma-separated family names
                for font in line.split(","):
                    fonts.add(font.strip())
            return fonts

    except Exception as e:
        logger.warning("system_font_list_failed", error=str(e))

    return set()


def check_missing_fonts(ppt_path: str) -> dict:
    """
    Check which fonts from the PPT are missing on the system.

    Returns:
        dict with:
            - used_fonts: fonts used in PPT
            - available_fonts: fonts available on system
            - missing_fonts: fonts that are missing
            - suggested_mappings: suggested font replacements
    """
    used_fonts = extract_fonts_from_ppt(ppt_path)
    system_fonts = get_system_fonts()

    missing_fonts = set()
    suggested_mappings = {}

    for font in used_fonts:
        if font and font not in system_fonts:
            # Check if there's a known mapping
            mapped_font = FONT_MAPPING.get(font)
            if mapped_font and mapped_font in system_fonts:
                suggested_mappings[font] = mapped_font
            else:
                missing_fonts.add(font)

    result = {
        "used_fonts": list(used_fonts),
        "available_fonts": len(system_fonts),
        "missing_fonts": list(missing_fonts),
        "suggested_mappings": suggested_mappings,
    }

    logger.info("font_check_complete", **result)
    return result


def create_libreoffice_font_substitution() -> str:
    """
    Create LibreOffice font substitution configuration.
    Returns the path to the configuration file.
    """
    # LibreOffice user profile path
    lo_profile = Path.home() / ".config" / "libreoffice" / "4" / "user"
    lo_profile.mkdir(parents=True, exist_ok=True)

    registrymodifications_path = lo_profile / "registrymodifications.xcu"

    # Create font substitution entries
    substitutions = []
    for original, replacement in FONT_MAPPING.items():
        substitutions.append(f'<item oor:path="/org.openoffice.Office.Common/Font/Substitution/FontPairs">'
                           f'<node oor:name="{original}" oor:op="replace">'
                           f'<prop oor:name="ReplaceFont"><value>{replacement}</value></prop>'
                           f'<prop oor:name="Always"><value>true</value></prop>'
                           f'</node></item>')

    config_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
{''.join(substitutions)}
</oor:items>
'''

    try:
        registrymodifications_path.write_text(config_content)
        logger.info("libreoffice_font_config_created", path=str(registrymodifications_path))
        return str(registrymodifications_path)
    except Exception as e:
        logger.error("libreoffice_font_config_failed", error=str(e))
        return ""
