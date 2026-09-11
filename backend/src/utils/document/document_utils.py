import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import unicodedata
from collections import defaultdict
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Literal

import pymupdf

from ...models.document_model import (
    PageAnalysis,
    PageExtractionResult,
    ParserState,
)


logger = logging.getLogger(__name__)


MAX_CHARS = 1000
OCR_LANGUAGE = "ind"
OCR_DPI = 300
OCR_MAX_WORKERS = 4
NATIVE_TEXT_MIN_CHARS = 200
IMAGE_COVERAGE_THRESHOLD = 0.70
# Density thresholds are intentionally conservative: on an A4 page they
# correspond to roughly 580 and 770 normalized characters, respectively.
IMAGE_DOMINANT_MIN_TEXT_DENSITY = 6.0
MULTI_IMAGE_COVERAGE_THRESHOLD = 0.50
MULTI_IMAGE_NATIVE_MAX_CHARS = 400
MULTI_IMAGE_MIN_TEXT_DENSITY = 8.0
MARGIN_SCAN_LINES = 5

PASAL_PATTERN = re.compile(
    r"^Pasal\s+(\d+[A-Za-z]?)(?:\s+(.+))?$",
    re.IGNORECASE,
)
AYAT_PATTERN = re.compile(r"^\((\d+)\)\s*(.*)$")


_OCR_WORKER_DOCUMENT: Any | None = None


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


def get_page_image_infos(page: Any, page_number: int) -> list[Any]:
    """Inspect page images and fail instead of guessing the extraction mode."""
    try:
        return list(page.get_image_info())
    except Exception as error:
        raise RuntimeError(
            f"Tidak dapat memeriksa image pada halaman {page_number}"
        ) from error


def normalized_text_length(text: str) -> int:
    """Count readable native characters after collapsing PDF whitespace."""
    return len(re.sub(r"\s+", " ", text).strip())


def image_union_coverage(page: Any, image_infos: list[Any]) -> float:
    """Return the exact union of visible image rectangles over the page area."""
    if not image_infos:
        return 0.0
    page_rect = pymupdf.Rect(page.rect)
    page_area = page_rect.get_area()
    if page_area <= 0:
        return 0.0

    rectangles: list[tuple[float, float, float, float]] = []
    for image_info in image_infos:
        if not isinstance(image_info, dict) or image_info.get("bbox") is None:
            continue
        try:
            image_rect = pymupdf.Rect(image_info["bbox"])
            visible_rect = image_rect & page_rect
            coordinates = (
                float(visible_rect.x0),
                float(visible_rect.y0),
                float(visible_rect.x1),
                float(visible_rect.y1),
            )
            if (
                visible_rect.get_area() > 0
                and all(math.isfinite(value) for value in coordinates)
            ):
                rectangles.append(coordinates)
        except (TypeError, ValueError):
            continue

    if not rectangles:
        return 0.0

    # Image counts on PDF pages are normally small. A vertical sweep is exact,
    # avoids double-counting overlaps, and needs no geometry dependency.
    x_coordinates = sorted({value for rect in rectangles for value in (rect[0], rect[2])})
    union_area = 0.0
    for left, right in zip(x_coordinates, x_coordinates[1:]):
        if right <= left:
            continue
        y_intervals = sorted(
            (top, bottom)
            for x0, top, x1, bottom in rectangles
            if x0 < right and x1 > left
        )
        covered_height = 0.0
        if y_intervals:
            current_top, current_bottom = y_intervals[0]
            for top, bottom in y_intervals[1:]:
                if top <= current_bottom:
                    current_bottom = max(current_bottom, bottom)
                else:
                    covered_height += current_bottom - current_top
                    current_top, current_bottom = top, bottom
            covered_height += current_bottom - current_top
        union_area += (right - left) * covered_height

    return min(1.0, union_area / page_area)


def largest_image_coverage(page: Any, image_infos: list[Any]) -> float:
    """Backward-compatible name; coverage now uses the union of all images."""
    return image_union_coverage(page, image_infos)


def page_text_density(page: Any, native_chars: int) -> float:
    """Return normalized native characters per square inch of page area."""
    if not hasattr(page, "rect"):
        return math.inf if native_chars else 0.0
    page_area_points = pymupdf.Rect(page.rect).get_area()
    if page_area_points <= 0:
        return 0.0
    return native_chars / (page_area_points / (72.0 * 72.0))


def has_suspicious_native_text(text: str) -> bool:
    """Detect strong corruption signals, not merely short legitimate text."""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if "\ufffd" in text or "\x00" in text or "(cid:" in text.casefold():
        return True

    alphanumeric_ratio = sum(character.isalnum() for character in compact) / len(compact)
    tokens = re.findall(r"\S+", text)
    single_character_ratio = (
        sum(len(token.strip(".,;:()[]{}")) == 1 for token in tokens) / len(tokens)
        if tokens
        else 0.0
    )
    return (
        len(compact) >= 40
        and (alphanumeric_ratio < 0.45 or (len(tokens) >= 20 and single_character_ratio > 0.45))
    )


def legal_structure_counts(text: str) -> tuple[int, int]:
    """Count high-precision Pasal and Ayat markers in normalized native lines."""
    lines = [normalize_line(line) for line in text.splitlines()]
    lines = join_split_pasal_lines([line for line in lines if line])
    return (
        sum(bool(PASAL_PATTERN.match(line)) for line in lines),
        sum(bool(AYAT_PATTERN.match(line)) for line in lines),
    )


def analyze_page(
    page: Any,
    page_number: int,
    *,
    native_text: str | None = None,
    image_infos: list[Any] | None = None,
    native_text_min_chars: int = NATIVE_TEXT_MIN_CHARS,
    image_coverage_threshold: float = IMAGE_COVERAGE_THRESHOLD,
) -> PageAnalysis:
    """Extract native signals once and make a maintainable OCR decision."""
    if native_text is None:
        native_text = page.get_text("text", sort=True)
    if image_infos is None:
        image_infos = get_page_image_infos(page, page_number)

    native_chars = normalized_text_length(native_text)
    image_coverage = image_union_coverage(page, image_infos)
    text_density = page_text_density(page, native_chars)
    native_pasal_count, native_ayat_count = legal_structure_counts(native_text)
    suspicious_text = has_suspicious_native_text(native_text)
    image_count = len(image_infos)

    should_ocr = False
    reason = "native_text_trustworthy"
    if suspicious_text:
        should_ocr = True
        reason = "suspicious_or_broken_native_text"
    elif native_chars == 0 and image_count > 0:
        should_ocr = True
        reason = "no_native_text_with_images"
    elif (
        image_coverage >= image_coverage_threshold
        and (
            native_chars < native_text_min_chars
            or text_density < IMAGE_DOMINANT_MIN_TEXT_DENSITY
        )
    ):
        should_ocr = True
        reason = "image_dominant_with_low_native_density"
    elif (
        image_count >= 2
        and image_coverage >= MULTI_IMAGE_COVERAGE_THRESHOLD
        and (
            native_chars < MULTI_IMAGE_NATIVE_MAX_CHARS
            or text_density < MULTI_IMAGE_MIN_TEXT_DENSITY
        )
    ):
        should_ocr = True
        reason = "multiple_images_with_incomplete_native_text"

    return PageAnalysis(
        page_number=page_number,
        native_text=native_text,
        native_chars=native_chars,
        text_density=text_density,
        image_count=image_count,
        image_coverage=image_coverage,
        native_pasal_count=native_pasal_count,
        native_ayat_count=native_ayat_count,
        suspicious_text=suspicious_text,
        should_ocr=should_ocr,
        decision_reason=reason,
    )


def should_use_full_page_ocr(
    page: Any,
    native_text: str,
    image_infos: list[Any],
    *,
    native_text_min_chars: int = NATIVE_TEXT_MIN_CHARS,
    image_coverage_threshold: float = IMAGE_COVERAGE_THRESHOLD,
) -> tuple[bool, int, float]:
    """Compatibility wrapper around the richer one-pass page analysis."""
    analysis = analyze_page(
        page,
        0,
        native_text=native_text,
        image_infos=image_infos,
        native_text_min_chars=native_text_min_chars,
        image_coverage_threshold=image_coverage_threshold,
    )
    return analysis.should_ocr, analysis.native_chars, analysis.image_coverage


def force_full_page_ocr(
    page: Any,
    page_number: int,
    *,
    ocr_language: str = OCR_LANGUAGE,
    ocr_dpi: int = OCR_DPI,
) -> str:
    """Run full-page OCR unconditionally after classification selected OCR."""
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


def extract_page_text(
    page: Any,
    page_number: int,
    *,
    ocr_language: str = OCR_LANGUAGE,
    ocr_dpi: int = OCR_DPI,
    image_infos: list[Any] | None = None,
) -> str:
    """Prefer native text and OCR only low-text pages dominated by an image."""
    if image_infos is None:
        image_infos = get_page_image_infos(page, page_number)

    native_text = page.get_text("text", sort=True)
    analysis = analyze_page(
        page,
        page_number,
        native_text=native_text,
        image_infos=image_infos,
    )
    if not analysis.should_ocr:
        return native_text

    return force_full_page_ocr(
        page,
        page_number,
        ocr_language=ocr_language,
        ocr_dpi=ocr_dpi,
    )


def initialize_ocr_worker(pdf_path: str) -> None:
    """Open one process-local PDF and keep Tesseract single-threaded per worker."""
    global _OCR_WORKER_DOCUMENT
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    _OCR_WORKER_DOCUMENT = pymupdf.open(pdf_path)


def extract_ocr_page_in_worker(
    page_number: int,
    ocr_language: str,
    ocr_dpi: int,
) -> str:
    """OCR one page without sharing a PyMuPDF document between processes."""
    if _OCR_WORKER_DOCUMENT is None:
        raise RuntimeError("OCR worker belum memiliki dokumen PDF")

    page = _OCR_WORKER_DOCUMENT[page_number - 1]
    return force_full_page_ocr(
        page,
        page_number,
        ocr_language=ocr_language,
        ocr_dpi=ocr_dpi,
    )


def collect_page_results_in_order(
    total_pages: int,
    native_page_texts: dict[int, str],
    ocr_page_futures: dict[int, Future[str]],
    on_page_progress: Callable[[int, int, str], None] | None = None,
    analyses: dict[int, PageAnalysis] | None = None,
) -> list[tuple[int, str]]:
    """Resolve parallel OCR into deterministic page order before stateful parsing."""
    pages: list[tuple[int, str]] = []
    for page_number in range(1, total_pages + 1):
        if page_number in ocr_page_futures:
            text = ocr_page_futures[page_number].result()
            extraction_mode = "full_ocr"
        else:
            text = native_page_texts[page_number]
            extraction_mode = "native_text"

        pages.append((page_number, text))
        if analyses is not None:
            analysis = analyses[page_number]
            analysis.final_extraction_mode = extraction_mode
            analysis.final_chars = normalized_text_length(text)
            logger.debug(
                "PDF page diagnostic | page_number=%d native_chars=%d "
                "image_count=%d image_coverage=%.3f text_density=%.2f "
                "ocr_decision=%s decision_reason=%s native_pasal_count=%d "
                "native_ayat_count=%d final_extraction_mode=%s final_chars=%d",
                page_number,
                analysis.native_chars,
                analysis.image_count,
                analysis.image_coverage,
                analysis.text_density,
                analysis.should_ocr,
                analysis.decision_reason,
                analysis.native_pasal_count,
                analysis.native_ayat_count,
                extraction_mode,
                analysis.final_chars,
            )
        if on_page_progress is not None:
            on_page_progress(page_number, total_pages, extraction_mode)

    return pages


def normalize_line(raw_line: str) -> str:
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


def line_key(line: str) -> str:
    """Return a case-insensitive key for repeated margin detection."""
    return re.sub(r"\s+", " ", line).strip().casefold()


def is_page_marker(line: str) -> bool:
    """Recognize common printed page-number variants."""
    return bool(
        re.match(r"^(?:[-–—]\s*)?\d{1,4}(?:\s*[-–—])?$", line)
        or re.match(r"^(?:halaman|page)\s*\d{1,4}$", line, re.IGNORECASE)
    )


def is_structural_line(line: str) -> bool:
    """Do not treat legal headings as repeated header/footer noise."""
    return bool(
        re.match(r"^BAB\s+", line, re.IGNORECASE)
        or re.match(r"^Bagian\s+", line, re.IGNORECASE)
        or re.match(r"^Paragraf\s+", line, re.IGNORECASE)
        or re.match(r"^Pasal\s+", line, re.IGNORECASE)
        or re.match(r"^\(\d+\)", line)
        or line.upper() == "PENJELASAN"
    )


def is_ocr_noise(line: str) -> bool:
    """Drop lines that contain no useful lexical content after OCR."""
    if not line or is_page_marker(line):
        return True
    compact_line = re.sub(r"\s+", "", line)
    return bool(re.match(r"^[|¦~_=*•·….,;:]+$", compact_line))


def basic_clean_page_lines(text: str) -> list[str]:
    """Clean one page without relying on neighboring pages."""
    clean_lines: list[str] = []
    ignored_headers = {
        "PRESIDEN",
        "REPUBLIK INDONESIA",
        "PRESIDEN REPUBLIK INDONESIA",
    }

    normalized_lines = join_split_pasal_lines(
        [normalize_line(raw_line) for raw_line in text.splitlines()]
    )
    for line in normalized_lines:
        if is_ocr_noise(line):
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


def join_hyphenated_lines(lines: list[str]) -> list[str]:
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


def join_split_pasal_lines(lines: list[str]) -> list[str]:
    """Safely join an isolated `Pasal` with a following legal identifier."""
    joined: list[str] = []
    index = 0
    while index < len(lines):
        if (
            lines[index].casefold() == "pasal"
            and index + 1 < len(lines)
            and re.fullmatch(r"\d+[A-Za-z]?", lines[index + 1])
        ):
            joined.append(f"Pasal {lines[index + 1]}")
            index += 2
            continue
        joined.append(lines[index])
        index += 1
    return joined


def clean_page_lines(
    text: str,
    *,
    repeated_margin_keys: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Normalize extracted lines and remove OCR, page, and margin noise."""
    lines = basic_clean_page_lines(text)
    repeated_margin_keys = repeated_margin_keys or set()
    if repeated_margin_keys:
        last_index = len(lines) - 1
        lines = [
            line
            for index, line in enumerate(lines)
            if not (
                line_key(line) in repeated_margin_keys
                and (
                    index < MARGIN_SCAN_LINES
                    or index >= last_index - MARGIN_SCAN_LINES + 1
                )
            )
        ]
    return join_hyphenated_lines(lines)


def find_repeated_margin_keys(page_lines: list[list[str]]) -> set[str]:
    """Find non-structural lines repeated in the top/bottom margin of pages."""
    occurrences: defaultdict[str, set[int]] = defaultdict(set)
    for page_index, lines in enumerate(page_lines):
        margin_lines = lines[:MARGIN_SCAN_LINES] + lines[-MARGIN_SCAN_LINES:]
        for line in margin_lines:
            key = line_key(line)
            if (
                len(line) >= 3
                and len(line) <= 120
                and not is_structural_line(line)
            ):
                occurrences[key].add(page_index)

    # A line must occur on at least two pages to avoid deleting legitimate
    # content that happens to sit at the top or bottom of one page.
    return {key for key, pages in occurrences.items() if len(pages) >= 2}


def clean_document_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Clean pages and remove headers/footers repeated across the document."""
    basic_pages = [
        (page_number, basic_clean_page_lines(text))
        for page_number, text in pages
    ]
    repeated_margin_keys = find_repeated_margin_keys(
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
        for line in join_split_pasal_lines(clean_page_lines(text)):
            bab_match = re.match(r"^BAB\s+([IVXLCDM]+)$", line, re.IGNORECASE)
            bagian_match = re.match(r"^Bagian\s+(.+)$", line, re.IGNORECASE)
            paragraf_match = re.match(r"^Paragraf\s+(\d+)$", line, re.IGNORECASE)
            pasal_match = PASAL_PATTERN.match(line)
            ayat_match = AYAT_PATTERN.match(line)
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
                identifier = pasal_match.group(1)
                state.current_pasal = (
                    f"{identifier[:-1]}{identifier[-1].upper()}"
                    if identifier[-1].isalpha()
                    else identifier
                )
                state.current_ayat = None
                state.waiting_title = None
                inline_text = (pasal_match.group(2) or "").strip()
                if inline_text:
                    state.current_lines.append(inline_text)
                    state.current_pages.append(page_number)
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


def extract_pdf_pages(
    pdf_path: str | Path,
    *,
    mode: Literal["native", "hybrid", "full_ocr"] = "hybrid",
    ocr_language: str = OCR_LANGUAGE,
    ocr_dpi: int = OCR_DPI,
    ocr_max_workers: int = OCR_MAX_WORKERS,
    on_page_progress: Callable[[int, int, str], None] | None = None,
) -> PageExtractionResult:
    """Analyze once, then extract pages in native, hybrid, or forced OCR mode."""
    pdf_file = Path(pdf_path)
    if not pdf_file.is_file():
        raise FileNotFoundError(f"PDF tidak ditemukan: {pdf_file}")
    if pdf_file.suffix.lower() != ".pdf":
        raise ValueError("File yang diproses harus berformat PDF")
    if ocr_max_workers < 1:
        raise ValueError("Jumlah OCR worker minimal 1")
    if mode not in {"native", "hybrid", "full_ocr"}:
        raise ValueError(f"Mode ekstraksi PDF tidak dikenal: {mode}")

    native_page_texts: dict[int, str] = {}
    ocr_pages: list[int] = []
    analyses: dict[int, PageAnalysis] = {}

    with pymupdf.open(pdf_file) as document:
        total_pages = len(document)
        for page_number, page in enumerate(document, start=1):
            image_infos = get_page_image_infos(page, page_number)
            native_text = page.get_text("text", sort=True)
            analysis = analyze_page(
                page,
                page_number,
                native_text=native_text,
                image_infos=image_infos,
            )
            if mode == "native":
                analysis.should_ocr = False
                analysis.decision_reason = "mode_native_only"
            elif mode == "full_ocr":
                analysis.should_ocr = True
                analysis.decision_reason = "mode_forced_full_ocr"
            analyses[page_number] = analysis

            if analysis.should_ocr:
                ocr_pages.append(page_number)
            else:
                native_page_texts[page_number] = native_text

    if ocr_pages:
        validate_ocr_dependencies(ocr_language)
        worker_count = min(ocr_max_workers, len(ocr_pages))
        spawn_context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=spawn_context,
            initializer=initialize_ocr_worker,
            initargs=(str(pdf_file),),
        ) as executor:
            ocr_page_futures = {
                page_number: executor.submit(
                    extract_ocr_page_in_worker,
                    page_number,
                    ocr_language,
                    ocr_dpi,
                )
                for page_number in ocr_pages
            }
            pages = collect_page_results_in_order(
                total_pages,
                native_page_texts,
                ocr_page_futures,
                on_page_progress,
                analyses,
            )
    else:
        pages = collect_page_results_in_order(
            total_pages,
            native_page_texts,
            {},
            on_page_progress,
            analyses,
        )

    return PageExtractionResult(
        pages=pages,
        analyses=[analyses[number] for number in range(1, total_pages + 1)],
    )


def calculate_document_metrics(
    pages: list[tuple[int, str]],
    chunks: list[dict[str, Any]],
    analyses: list[PageAnalysis] | None = None,
) -> dict[str, Any]:
    """Calculate coverage and legal-structure metrics without using chunks as truth."""
    normalized_pages = clean_document_pages(pages)
    normalized_characters = sum(
        normalized_text_length(text) for _, text in normalized_pages
    )
    pasal_count = 0
    ayat_count = 0
    identifiers: set[str] = set()
    text_pages: set[int] = set()
    current_pasal: str | None = None
    for page_number, text in normalized_pages:
        if normalized_text_length(text):
            text_pages.add(page_number)
        lines = join_split_pasal_lines(clean_page_lines(text))
        for line in lines:
            if pasal_match := PASAL_PATTERN.match(line):
                current_pasal = pasal_match.group(1).upper()
                pasal_count += 1
                identifiers.add(f"Pasal {current_pasal}")
            elif current_pasal and (ayat_match := AYAT_PATTERN.match(line)):
                ayat_count += 1
                identifiers.add(f"Pasal {current_pasal} Ayat {ayat_match.group(1)}")

    total_pages = len(pages)
    chunk_pages = {
        int(page_number)
        for chunk in chunks
        for page_number in chunk.get("pages", [])
    }
    return {
        "normalized_characters": normalized_characters,
        "pasal_count": pasal_count,
        "ayat_count": ayat_count,
        "detected_legal_identifiers": sorted(identifiers),
        "page_coverage": len(text_pages) / total_pages if total_pages else 0.0,
        "chunk_page_coverage": len(chunk_pages) / total_pages if total_pages else 0.0,
        "chunk_count": len(chunks),
        "ocr_pages": sum(
            analysis.final_extraction_mode == "full_ocr"
            for analysis in (analyses or [])
        ),
        "native_pages": sum(
            analysis.final_extraction_mode == "native_text"
            for analysis in (analyses or [])
        ),
    }


def process_pdf_document(
    pdf_path: str | Path,
    output_path: str | Path | None = None,
    *,
    ocr_language: str = OCR_LANGUAGE,
    ocr_dpi: int = OCR_DPI,
    ocr_max_workers: int = OCR_MAX_WORKERS,
    max_chars: int = MAX_CHARS,
    on_page_progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Extract pages concurrently, then parse them sequentially in page order."""
    pdf_file = Path(pdf_path)
    extraction = extract_pdf_pages(
        pdf_file,
        mode="hybrid",
        ocr_language=ocr_language,
        ocr_dpi=ocr_dpi,
        ocr_max_workers=ocr_max_workers,
        on_page_progress=on_page_progress,
    )

    chunks = parse_document_pages(
        extraction.pages,
        filename=pdf_file.name,
        max_chars=max_chars,
    )
    metrics = calculate_document_metrics(
        extraction.pages,
        chunks,
        extraction.analyses,
    )
    logger.info(
        "PDF extraction summary | filename=%s normalized_characters=%d "
        "pasal_count=%d ayat_count=%d chunks=%d page_coverage=%.3f "
        "chunk_page_coverage=%.3f native_pages=%d ocr_pages=%d total_pages=%d",
        pdf_file.name,
        metrics["normalized_characters"],
        metrics["pasal_count"],
        metrics["ayat_count"],
        metrics["chunk_count"],
        metrics["page_coverage"],
        metrics["chunk_page_coverage"],
        metrics["native_pages"],
        metrics["ocr_pages"],
        len(extraction.pages),
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
