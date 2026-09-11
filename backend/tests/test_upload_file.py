import unittest

from src.services.document_service import (
    _collect_page_results_in_order,
    clean_document_pages,
    clean_page_lines,
    extract_page_text,
    parse_document_pages,
)


class TestTextCleaning(unittest.TestCase):
    def test_normalizes_noise_and_joins_hyphenated_words(self):
        text = "PRESIDEN\nketenaga-\nkerjaan\u00a0Indonesia\n- 12 -\n| |"

        self.assertEqual(
            clean_page_lines(text),
            ["ketenagakerjaan Indonesia"],
        )

    def test_removes_repeated_header_but_preserves_legal_headings(self):
        pages = [
            (
                1,
                "Sekretariat Negara\nBAB I\nPasal 1\n(1) Ketentuan pertama\n- 1 -",
            ),
            (
                2,
                "Sekretariat Negara\nPasal 2\n(1) Ketentuan kedua\n- 2 -",
            ),
        ]

        cleaned = clean_document_pages(pages)

        self.assertEqual(
            cleaned,
            [
                (1, "BAB I\nPasal 1\n(1) Ketentuan pertama"),
                (2, "Pasal 2\n(1) Ketentuan kedua"),
            ],
        )


class TestPageExtraction(unittest.TestCase):
    def test_parallel_ocr_results_are_collected_in_page_order(self):
        class CompletedOcr:
            def __init__(self, text):
                self.text = text

            def result(self):
                return self.text

        progress = []
        pages = _collect_page_results_in_order(
            3,
            {2: "lanjutan halaman kedua"},
            {
                3: CompletedOcr("Pasal 2\n(1) halaman ketiga"),
                1: CompletedOcr("Pasal 1\n(1) halaman pertama"),
            },
            lambda current, total, mode: progress.append((current, total, mode)),
        )

        self.assertEqual(
            pages,
            [
                (1, "Pasal 1\n(1) halaman pertama"),
                (2, "lanjutan halaman kedua"),
                (3, "Pasal 2\n(1) halaman ketiga"),
            ],
        )
        self.assertEqual(
            progress,
            [
                (1, 3, "full_ocr"),
                (2, 3, "native_text"),
                (3, 3, "full_ocr"),
            ],
        )

        chunks = parse_document_pages(pages, filename="aturan.pdf")
        self.assertEqual(
            chunks[0]["text"],
            "halaman pertama lanjutan halaman kedua",
        )

    def test_uses_native_text_without_running_ocr_when_page_has_no_image(self):
        class TextOnlyPage:
            def __init__(self):
                self.ocr_called = False

            def get_image_info(self):
                return []

            def get_textpage_ocr(self, **_kwargs):
                self.ocr_called = True
                raise AssertionError("OCR tidak boleh dipanggil")

            def get_text(self, _format, **kwargs):
                self.assert_no_textpage(kwargs)
                return "Pasal 1"

            @staticmethod
            def assert_no_textpage(kwargs):
                if "textpage" in kwargs:
                    raise AssertionError("Native extraction tidak memakai OCR textpage")

        page = TextOnlyPage()

        self.assertEqual(extract_page_text(page, 1), "Pasal 1")
        self.assertFalse(page.ocr_called)

    def test_uses_full_page_ocr_for_low_text_page_with_large_image(self):
        class ImagePage:
            def __init__(self):
                self.rect = (0, 0, 100, 100)
                self.ocr_arguments = None
                self.ocr_textpage = object()

            def get_image_info(self):
                return [{"xref": 1, "bbox": (0, 0, 100, 100)}]

            def get_textpage_ocr(self, **kwargs):
                self.ocr_arguments = kwargs
                return self.ocr_textpage

            def get_text(self, _format, **kwargs):
                if kwargs.get("textpage") is self.ocr_textpage:
                    return "Pasal 2 hasil OCR"
                return ""

        page = ImagePage()

        self.assertEqual(extract_page_text(page, 2), "Pasal 2 hasil OCR")
        self.assertEqual(
            page.ocr_arguments,
            {"language": "ind", "dpi": 300, "full": True},
        )

    def test_keeps_native_text_when_header_image_covers_less_than_70_percent(self):
        class NativePageWithHeader:
            rect = (0, 0, 100, 100)

            def __init__(self):
                self.ocr_called = False

            @staticmethod
            def get_image_info():
                return [{"xref": 1, "bbox": (0, 0, 100, 15)}]

            def get_textpage_ocr(self, **_kwargs):
                self.ocr_called = True
                raise AssertionError("Gambar header kecil tidak boleh memicu OCR")

            @staticmethod
            def get_text(_format, **_kwargs):
                return "Pasal 1\nTeks native singkat"

        page = NativePageWithHeader()

        self.assertEqual(extract_page_text(page, 1), "Pasal 1\nTeks native singkat")
        self.assertFalse(page.ocr_called)

    def test_keeps_native_text_above_200_chars_even_with_full_page_image(self):
        native_text = "Pasal 1\n" + ("ketenagakerjaan " * 20)

        class NativePageWithBackground:
            rect = (0, 0, 100, 100)

            def __init__(self):
                self.ocr_called = False

            @staticmethod
            def get_image_info():
                return [{"xref": 1, "bbox": (0, 0, 100, 100)}]

            def get_textpage_ocr(self, **_kwargs):
                self.ocr_called = True
                raise AssertionError("Native text yang cukup tidak boleh memicu OCR")

            @staticmethod
            def get_text(_format, **_kwargs):
                return native_text

        page = NativePageWithBackground()

        self.assertEqual(extract_page_text(page, 1), native_text)
        self.assertFalse(page.ocr_called)


class TestChunkIdentity(unittest.TestCase):
    def test_repeated_ayat_continues_part_number_instead_of_reusing_id(self):
        chunks = parse_document_pages(
            [
                (
                    1,
                    "Pasal 1\n(1) Isi pertama\n(2) Isi kedua\n"
                    "(1) Isi ayat satu yang terdeteksi kembali",
                )
            ],
            filename="aturan.pdf",
        )

        ayat_one_chunks = [chunk for chunk in chunks if chunk["ayat"] == "1"]
        self.assertEqual([chunk["part"] for chunk in ayat_one_chunks], [1, 2])
        self.assertEqual(
            len({chunk["chunk_id"] for chunk in chunks}),
            len(chunks),
        )


if __name__ == "__main__":
    unittest.main()
