import unittest

from src.services.document_service import (
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

    def test_uses_only_full_page_ocr_when_page_has_an_image(self):
        class ImagePage:
            def __init__(self):
                self.ocr_arguments = None
                self.ocr_textpage = object()

            def get_image_info(self):
                return [{"xref": 1}]

            def get_textpage_ocr(self, **kwargs):
                self.ocr_arguments = kwargs
                return self.ocr_textpage

            def get_text(self, _format, **kwargs):
                if kwargs.get("textpage") is not self.ocr_textpage:
                    raise AssertionError("Halaman image tidak boleh memakai native text")
                return "Pasal 2 hasil OCR"

        page = ImagePage()

        self.assertEqual(extract_page_text(page, 2), "Pasal 2 hasil OCR")
        self.assertEqual(
            page.ocr_arguments,
            {"language": "ind", "dpi": 300, "full": True},
        )


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
