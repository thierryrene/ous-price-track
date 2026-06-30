from __future__ import annotations

import unittest

from ous_monitor.filters import is_tenis, should_keep


class FilterTests(unittest.TestCase):
    def test_gender_blocks_feminine_and_kids_terms(self):
        self.assertEqual(should_keep("Tênis Adidas Feminino", ["42"]), (False, "gender"))
        self.assertEqual(should_keep("Chuteira Umbro Junior", ["42"]), (False, "gender"))

    def test_tenis_size_filter_requires_wanted_size_when_sizes_exist(self):
        self.assertTrue(is_tenis("Tênis Umbro Speciali"))
        self.assertEqual(should_keep("Tênis Umbro Speciali", ["40", "41"]), (False, "size"))
        self.assertEqual(should_keep("Tênis Umbro Speciali", ["41", "42"]), (True, ""))

    def test_tenis_without_sizes_is_kept(self):
        self.assertEqual(should_keep("Tênis OUS Imigrante", []), (True, ""))


if __name__ == "__main__":
    unittest.main()
