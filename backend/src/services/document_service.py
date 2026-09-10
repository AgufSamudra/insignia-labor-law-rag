import json
import logging
import re
import shutil
import subprocess
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pymupdf


logger = logging.getLogger(__name__)


MAX_CHARS = 1000
OCR_LANGUAGE = "ind"
OCR_DPI = 300
MARGIN_SCAN_LINES = 5


@dataclass
class ParserState:
    """Keep the legal document hierarchy while pages are being parsed."""

    current_bab: str | None = None
    current_bab_title: str | None = None
    current_pasal: str | None = None
    current_ayat: str | None = None
    current_lines: list[str] = field(default_factory=list)
    current_pages: list[int] = field(default_factory=list)
    waiting_title: str | None = None
    section_part_counters: dict[tuple[str, str | None], int] = field(
        default_factory=dict
    )


def validate_ocr_dependencies(language: str = OCR_LANGUAGE) -> str:
    """Validate Tesseract and return its executable path for OCR processing."""
    tesseract_path = shutil.which("tesseract")
    if not tesseract_path:
        raise RuntimeError(
            "Tesseract tidak ditemukan. Install terlebih dahulu: "
            "apt-get install -y tesseract-ocr tesseract-ocr-ind"
        )

    result = subprocess.run(
        [tesseract_path, "--list-langs"],
        capture_output=True,
        text=True,
        check=True,
    )
    installed_languages = {
        item.strip() for item in result.stdout.splitlines() if item.strip()
    }
    if language not in installed_languages:
        raise RuntimeError(
            f"Language pack Tesseract '{language}' tidak ditemukan. "
            "Install dengan: apt-get install -y tesseract-ocr-ind"
        )

    return tesseract_path


def _get_page_image_infos(page: Any, page_number: int) -> list[Any]:
    """Inspect page images and fail instead of guessing the extraction mode."""
    try:
        return list(page.get_image_info())
    except Exception as error:
        raise RuntimeError(
            f"Tidak dapat memeriksa image pada halaman {page_number}"
        ) from error


def extract_page_text(
    page: Any,
    page_number: int,
    *,
    ocr_language: str = OCR_LANGUAGE,
    ocr_dpi: int = OCR_DPI,
    image_infos: list[Any] | None = None,
) -> str:
    """Use native text for text-only pages and full OCR for pages with images."""
    if image_infos is None:
        image_infos = _get_page_image_infos(page, page_number)

    if not image_infos:
        logger.info("Native text extraction | page=%d", page_number)
        return page.get_text("text", sort=True)

    logger.info(
        "OCR started | page=%d images=%d",
        page_number,
        len(image_infos),
    )
    try:
        text_page = page.get_textpage_ocr(
            language=ocr_language,
            dpi=ocr_dpi,
            full=True,
        )
        ocr_text = page.get_text("text", textpage=text_page, sort=True)
    except Exception as error:
        raise RuntimeError(
            f"Full-page OCR gagal pada halaman {page_number}"
        ) from error
    if not ocr_text.strip():
        raise RuntimeError(
            f"Full-page OCR tidak menghasilkan teks pada halaman {page_number}"
        )
    return ocr_text


def _normalize_line(raw_line: str) -> str:
    """Normalize Unicode, whitespace, and control characters from extraction/OCR."""
    line = unicodedata.normalize("NFKC", raw_line)
    line = line.replace("\u00a0", " ").replace("\u00ad", "")
    line = line.replace("\ufffd", " ")
    line = "".join(
        character
        for character in line
        if not unicodedata.category(character).startswith("C")
    )
    return re.sub(r"\s+", " ", line).strip()


def _line_key(line: str) -> str:
    """Return a case-insensitive key for repeated margin detection."""
    return re.sub(r"\s+", " ", line).strip().casefold()


def _is_page_marker(line: str) -> bool:
    """Recognize common printed page-number variants."""
    return bool(
        re.match(r"^(?:[-–—]\s*)?\d{1,4}(?:\s*[-–—])?$", line)
        or re.match(r"^(?:halaman|page)\s*\d{1,4}$", line, re.IGNORECASE)
    )


def _is_structural_line(line: str) -> bool:
    """Do not treat legal headings as repeated header/footer noise."""
    return bool(
        re.match(r"^BAB\s+", line, re.IGNORECASE)
        or re.match(r"^Bagian\s+", line, re.IGNORECASE)
        or re.match(r"^Paragraf\s+", line, re.IGNORECASE)
        or re.match(r"^Pasal\s+", line, re.IGNORECASE)
        or re.match(r"^\(\d+\)", line)
        or line.upper() == "PENJELASAN"
    )


def _is_ocr_noise(line: str) -> bool:
    """Drop lines that contain no useful lexical content after OCR."""
    if not line or _is_page_marker(line):
        return True
    compact_line = re.sub(r"\s+", "", line)
    return bool(re.match(r"^[|¦~_=*•·….,;:]+$", compact_line))


def _basic_clean_page_lines(text: str) -> list[str]:
    """Clean one page without relying on neighboring pages."""
    clean_lines: list[str] = []
    ignored_headers = {
        "PRESIDEN",
        "REPUBLIK INDONESIA",
        "PRESIDEN REPUBLIK INDONESIA",
    }

    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if _is_ocr_noise(line):
            continue

        upper_line = line.upper()
        if upper_line in ignored_headers:
            continue
        if re.match(r"^-\s*\d+\s*-\s*$", line):
            continue
        if re.match(r"^SK\s+No\.?\s*", line, re.IGNORECASE):
            continue

        clean_lines.append(line)

    return clean_lines


def _join_hyphenated_lines(lines: list[str]) -> list[str]:
    """Undo word breaks introduced when PDF text wraps at a hyphen."""
    joined: list[str] = []
    for line in lines:
        if (
            joined
            and re.search(r"[A-Za-zÀ-ÿ]-$", joined[-1])
            and re.match(r"^[a-zà-ÿ]", line)
        ):
            joined[-1] = joined[-1][:-1] + line
        else:
            joined.append(line)
    return joined


def clean_page_lines(
    text: str,
    *,
    repeated_margin_keys: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Normalize extracted lines and remove OCR, page, and margin noise."""
    lines = _basic_clean_page_lines(text)
    repeated_margin_keys = repeated_margin_keys or set()
    if repeated_margin_keys:
        last_index = len(lines) - 1
        lines = [
            line
            for index, line in enumerate(lines)
            if not (
                _line_key(line) in repeated_margin_keys
                and (
                    index < MARGIN_SCAN_LINES
                    or index >= last_index - MARGIN_SCAN_LINES + 1
                )
            )
        ]
    return _join_hyphenated_lines(lines)


def _find_repeated_margin_keys(page_lines: list[list[str]]) -> set[str]:
    """Find non-structural lines repeated in the top/bottom margin of pages."""
    occurrences: defaultdict[str, set[int]] = defaultdict(set)
    for page_index, lines in enumerate(page_lines):
        margin_lines = lines[:MARGIN_SCAN_LINES] + lines[-MARGIN_SCAN_LINES:]
        for line in margin_lines:
            key = _line_key(line)
            if (
                len(line) >= 3
                and len(line) <= 120
                and not _is_structural_line(line)
            ):
                occurrences[key].add(page_index)

    # A line must occur on at least two pages to avoid deleting legitimate
    # content that happens to sit at the top or bottom of one page.
    return {key for key, pages in occurrences.items() if len(pages) >= 2}


def clean_document_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Clean pages and remove headers/footers repeated across the document."""
    basic_pages = [
        (page_number, _basic_clean_page_lines(text))
        for page_number, text in pages
    ]
    repeated_margin_keys = _find_repeated_margin_keys(
        [lines for _, lines in basic_pages]
    )
    return [
        (
            page_number,
            "\n".join(
                clean_page_lines(
                    "\n".join(lines),
                    repeated_margin_keys=repeated_margin_keys,
                )
            ),
        )
        for page_number, lines in basic_pages
    ]


def make_document_id(filename: str) -> str:
    """Create a stable identifier that is safe to use in chunk IDs and filenames."""
    document_id = re.sub(r"[^a-z0-9]+", "_", Path(filename).stem.lower()).strip("_")
    return document_id or "document"


def flush_current_section(
    state: ParserState,
    chunks: list[dict[str, Any]],
    *,
    filename: str,
    document_id: str,
    max_chars: int = MAX_CHARS,
) -> None:
    """Turn the current Pasal/Ayat text into page-aware chunks and clear the buffer."""
    if state.current_pasal is None or not state.current_lines:
        return

    word_items = [
        (word, page_number)
        for content_line, page_number in zip(state.current_lines, state.current_pages)
        for word in content_line.split()
    ]
    if not word_items:
        state.current_lines.clear()
        state.current_pages.clear()
        return

    section_key = (state.current_pasal, state.current_ayat)
    part = state.section_part_counters.get(section_key, 0) + 1
    temp_words: list[str] = []
    temp_pages: list[int] = []
    temp_length = 0
    ayat_id = f"ayat{state.current_ayat}" if state.current_ayat else "noayat"

    def save_chunk() -> None:
        if not temp_words or not temp_pages:
            return
        pages = sorted(set(temp_pages))
        chunks.append(
            {
                "filename": filename,
                "bab": state.current_bab,
                "bab_title": state.current_bab_title,
                "pasal": state.current_pasal,
                "ayat": state.current_ayat,
                "part": part,
                "page_start": pages[0],
                "page_end": pages[-1],
                "pages": pages,
                "text": " ".join(temp_words),
                "chunk_id": (
                    f"{document_id}-pasal{state.current_pasal}"
                    f"-{ayat_id}-part{part}"
                ),
            }
        )

    for word, page_number in word_items:
        word_length = len(word) + (1 if temp_words else 0)
        if temp_words and temp_length + word_length > max_chars:
            save_chunk()
            part += 1
            temp_words = []
            temp_pages = []
            temp_length = 0
            word_length = len(word)

        temp_words.append(word)
        temp_pages.append(page_number)
        temp_length += word_length

    save_chunk()
    state.section_part_counters[section_key] = part
    state.current_lines.clear()
    state.current_pages.clear()


def parse_document_pages(
    pages: list[tuple[int, str]],
    *,
    filename: str,
    max_chars: int = MAX_CHARS,
) -> list[dict[str, Any]]:
    """Parse legal headings and content from extracted pages into structured chunks."""
    state = ParserState()
    chunks: list[dict[str, Any]] = []
    document_id = make_document_id(filename)

    pages = clean_document_pages(pages)
    for page_number, text in pages:
        for line in clean_page_lines(text):
            bab_match = re.match(r"^BAB\s+([IVXLCDM]+)$", line, re.IGNORECASE)
            bagian_match = re.match(r"^Bagian\s+(.+)$", line, re.IGNORECASE)
            paragraf_match = re.match(r"^Paragraf\s+(\d+)$", line, re.IGNORECASE)
            pasal_match = re.match(r"^Pasal\s+(\d+[A-Za-z]?)$", line, re.IGNORECASE)
            ayat_match = re.match(r"^\((\d+)\)\s*(.*)$", line)
            is_penjelasan = line.upper() == "PENJELASAN"

            is_structure = (
                bab_match
                or bagian_match
                or paragraf_match
                or pasal_match
                or ayat_match
                or is_penjelasan
            )
            if is_structure:
                flush_current_section(
                    state,
                    chunks,
                    filename=filename,
                    document_id=document_id,
                    max_chars=max_chars,
                )

            if is_penjelasan:
                return chunks

            if bab_match:
                state.current_bab = bab_match.group(1).upper()
                state.current_bab_title = None
                state.current_pasal = None
                state.current_ayat = None
                state.waiting_title = "bab"
                continue

            if bagian_match:
                state.current_pasal = None
                state.current_ayat = None
                state.waiting_title = "bagian"
                continue

            if paragraf_match:
                state.current_pasal = None
                state.current_ayat = None
                state.waiting_title = "paragraf"
                continue

            if pasal_match:
                state.current_pasal = pasal_match.group(1)
                state.current_ayat = None
                state.waiting_title = None
                continue

            if ayat_match and state.current_pasal is not None:
                state.current_ayat = ayat_match.group(1)
                ayat_text = ayat_match.group(2).strip()
                if ayat_text:
                    state.current_lines.append(ayat_text)
                    state.current_pages.append(page_number)
                state.waiting_title = None
                continue

            if state.waiting_title == "bab":
                state.current_bab_title = (
                    f"{state.current_bab_title} {line}".strip()
                    if state.current_bab_title
                    else line
                )
                continue

            if state.waiting_title in {"bagian", "paragraf"}:
                continue

            if state.current_pasal is not None:
                state.current_lines.append(line)
                state.current_pages.append(page_number)

    flush_current_section(
        state,
        chunks,
        filename=filename,
        document_id=document_id,
        max_chars=max_chars,
    )
    return chunks


def process_pdf_document(
    pdf_path: str | Path,
    output_path: str | Path | None = None,
    *,
    ocr_language: str = OCR_LANGUAGE,
    ocr_dpi: int = OCR_DPI,
    max_chars: int = MAX_CHARS,
    on_page_progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Extract, parse, and optionally persist one PDF as structured JSONL chunks."""
    pdf_file = Path(pdf_path)
    if not pdf_file.is_file():
        raise FileNotFoundError(f"PDF tidak ditemukan: {pdf_file}")
    if pdf_file.suffix.lower() != ".pdf":
        raise ValueError("File yang diproses harus berformat PDF")

    pages: list[tuple[int, str]] = []
    ocr_dependencies_validated = False

    with pymupdf.open(pdf_file) as document:
        total_pages = len(document)
        for page_number, page in enumerate(document, start=1):
            image_infos = _get_page_image_infos(page, page_number)
            if image_infos and not ocr_dependencies_validated:
                validate_ocr_dependencies(ocr_language)
                ocr_dependencies_validated = True
            pages.append(
                (
                    page_number,
                    extract_page_text(
                        page,
                        page_number,
                        ocr_language=ocr_language,
                        ocr_dpi=ocr_dpi,
                        image_infos=image_infos,
                    ),
                )
            )
            if on_page_progress is not None:
                extraction_mode = "full_ocr" if image_infos else "native_text"
                on_page_progress(page_number, total_pages, extraction_mode)

    chunks = parse_document_pages(
        pages,
        filename=pdf_file.name,
        max_chars=max_chars,
    )
    if output_path is not None:
        write_chunks_jsonl(chunks, output_path)

    return chunks


def write_chunks_jsonl(chunks: list[dict[str, Any]], output_path: str | Path) -> Path:
    """Persist parsed chunks as UTF-8 JSON Lines and return the resulting path."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as file:
        for chunk in chunks:
            file.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return destination
