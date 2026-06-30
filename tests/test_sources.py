from __future__ import annotations

import unittest

from ous_monitor.sources import SOURCES, ci_source_keys, dashboard_source_config


class SourceRegistryTests(unittest.TestCase):
    def test_umbro_is_registered_for_ci_and_dashboard(self):
        self.assertIn("umbro", SOURCES)
        self.assertIn("umbro", ci_source_keys())
        self.assertEqual(dashboard_source_config()["umbro"]["label"], "Umbro Oficial")

    def test_centauro_is_removed(self):
        self.assertNotIn("centauro", SOURCES)

    def test_approve_is_excluded_from_ci(self):
        self.assertIn("approve", SOURCES)
        self.assertNotIn("approve", ci_source_keys())


if __name__ == "__main__":
    unittest.main()
