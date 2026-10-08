"""Cover controller profile validation failures."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from controllers.profiles import load_profile


class ProfileValidationTest(unittest.TestCase):
    """Reject malformed fader analog scales while loading a profile."""

    def test_rejects_invalid_analog_scales(self) -> None:
        bad_scales = (
            [0.0],
            [0.0, 1.0, 2.0],
            ["low", 1.0],
            [True, 1.0],
            [float("nan"), 1.0],
            [10**400, 1],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            for scale in bad_scales:
                with self.subTest(scale=scale):
                    data = {
                        "faders": {
                            "1": {
                                "key": "TEMPO",
                                "analog_scale": scale,
                            }
                        }
                    }
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "analog_scale"):
                        load_profile(path)

    def test_normalizes_integer_analog_scale_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            path.write_text(
                json.dumps({
                    "faders": {
                        "1": {
                            "key": "TEMPO",
                            "analog_scale": [0, 127],
                        }
                    }
                }),
                encoding="utf-8",
            )
            profile = load_profile(path)
        self.assertEqual(profile.faders[1].analog_scale, (0.0, 127.0))


if __name__ == "__main__":
    unittest.main()
