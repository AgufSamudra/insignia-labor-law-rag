import unittest

from src.services.embedding_service import _parse_sparse


class TestSparseParsing(unittest.TestCase):
    def test_coordinate_map_is_sorted_and_drops_zeroes(self):
        self.assertEqual(
            _parse_sparse({"12": 0.4, "2": 0.8, "9": 0}, 0),
            ([2, 12], [0.8, 0.4]),
        )


    def test_full_vocabulary_row(self):
        self.assertEqual(
            _parse_sparse([0, 0.25, 0, 0.75], 0),
            ([1, 3], [0.25, 0.75]),
        )
