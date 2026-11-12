import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_macos
from analyticsbot.gui_config import data_dir


class MacOSSupportTests(unittest.TestCase):
    def test_macos_data_lives_outside_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch("analyticsbot.gui_config.sys.platform", "darwin"), patch("analyticsbot.gui_config.Path.home", return_value=home):
                actual = data_dir()
            self.assertEqual(actual, home / "Library" / "Application Support" / "AnalyticsBot")
            self.assertTrue(actual.is_dir())

    def test_build_refuses_other_platforms(self):
        with patch("build_macos.sys.platform", "win32"), patch("build_macos.sys.argv", ["build_macos.py"]):
            with self.assertRaises(SystemExit) as error:
                build_macos.main()
        self.assertEqual(error.exception.code, 2)

    def test_release_contains_only_bundle_and_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MACOS_README.txt").write_text("Instructions", encoding="utf-8")
            (root / "user_data").mkdir()
            (root / "user_data" / "secret.json").write_text("secret", encoding="utf-8")
            bundle = root / "gui-build-macos" / "output" / "AnalyticsBot.app"
            (bundle / "Contents" / "MacOS").mkdir(parents=True)
            (bundle / "Contents" / "MacOS" / "AnalyticsBot").write_text("binary", encoding="utf-8")
            with patch("build_macos.__file__", str(root / "build_macos.py")), patch("build_macos.sys.platform", "darwin"), patch("build_macos.sys.argv", ["build_macos.py"]), patch("build_macos.subprocess.run") as run:
                build_macos.main()
            release = root / "gui-dist-macos" / "AnalyticsBot-macOS"
            self.assertEqual({p.name for p in release.iterdir()}, {"AnalyticsBot.app", "README.txt"})
            self.assertEqual(run.call_args_list[1].args[0][-1], "--self-test")
            self.assertEqual(run.call_args_list[2].args[0][0], "/usr/bin/ditto")


if __name__ == "__main__":
    unittest.main()
