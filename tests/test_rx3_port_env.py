"""Cover environment parsing for the bridge port."""

import unittest

from wsl.rx3_guard import DEFAULT_BRIDGE_PORT, parse_port


class BridgePortEnvironmentTest(unittest.TestCase):
    def test_default_port_is_shared(self):
        self.assertEqual(parse_port(str(DEFAULT_BRIDGE_PORT)), 4480)

    def test_valid_ascii_decimal_port(self):
        self.assertEqual(parse_port("65535"), 65535)

    def test_invalid_port_strings_rejected(self):
        for value in ("0", "1023", "65536", "4480x", "٤٤٨٠", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_port(value)


if __name__ == "__main__":
    unittest.main()
