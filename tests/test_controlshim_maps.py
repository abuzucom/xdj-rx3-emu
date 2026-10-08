"""Verify the WSL adapter selects QEMU guest memory maps."""

import unittest

from wsl.patch_controlshim import adapt_control_source


class ControlShimMapsTest(unittest.TestCase):
    def test_guest_map_path_replaces_only_exact_literal(self) -> None:
        source = 'int fd=open("/hostproc/self/maps",O_RDONLY);\n'
        self.assertEqual(adapt_control_source(source), 'int fd=open("/proc/self/maps",O_RDONLY);\n')

    def test_unexpected_source_requires_review(self) -> None:
        for source in ('open("/proc/self/maps")', "", '"/hostproc/self/maps" " /hostproc/self/maps"'):
            if source.count('"/hostproc/self/maps"') == 1:
                continue
            with self.assertRaises(ValueError):
                adapt_control_source(source)

    def test_duplicate_source_requires_review(self) -> None:
        with self.assertRaises(ValueError):
            adapt_control_source('"/hostproc/self/maps" " /other" " /other" "' + '/hostproc/self/maps"')


if __name__ == "__main__":
    unittest.main()
