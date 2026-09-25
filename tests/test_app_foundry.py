import json
import os
import tempfile
import unittest
from pathlib import Path

from app_foundry import AppFoundry, AppFoundryError


class AppFoundryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.foundry = AppFoundry(Path(self.temporary.name))

    def tearDown(self) -> None:
        for process in self.foundry._live.values():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        self.temporary.cleanup()

    def _create(self, content: str = "import time\ntime.sleep(5)\n") -> str:
        return self.foundry.create(
            "Focus Timer",
            "A small offline timer.",
            "main.py",
            [
                {"path": "main.py", "content": content},
                {"path": "settings.json", "content": '{"minutes": 25}'},
            ],
        )

    def test_creates_immutable_verified_version_and_manifest(self) -> None:
        result = self._create("import tkinter as tk\nroot = tk.Tk()\nroot.destroy()\n")
        self.assertIn("Verified app created", result)
        self.assertIn("not launched", result)
        current = next(self.foundry.root.glob("focus-timer/versions/*"))
        manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "verified")
        self.assertIn("ast-policy", manifest["gates"])
        self.assertEqual(len(manifest["files"]["main.py"]), 64)

    def test_new_build_adds_version_without_overwriting_old_one(self) -> None:
        self._create("value = 1\n")
        self._create("value = 2\n")
        versions = list(self.foundry.root.glob("focus-timer/versions/*"))
        self.assertEqual(len(versions), 2)

    def test_rejects_escape_unsafe_import_and_dynamic_execution(self) -> None:
        with self.assertRaisesRegex(AppFoundryError, "Unsafe"):
            self.foundry.create("Bad App", "", "../main.py", [{"path": "../main.py", "content": "x=1"}])
        with self.assertRaisesRegex(AppFoundryError, "import 'subprocess'"):
            self.foundry.create("Bad App", "", "main.py", [{"path": "main.py", "content": "import subprocess"}])
        with self.assertRaisesRegex(AppFoundryError, "call to 'eval'"):
            self.foundry.create("Bad App", "", "main.py", [{"path": "main.py", "content": "eval('1')"}])
        with self.assertRaisesRegex(AppFoundryError, "attribute 'call'"):
            self.foundry.create("Bad App", "", "main.py", [{
                "path": "main.py",
                "content": "import tkinter as tk\nroot=tk.Tk()\nroot.tk.call('exec', 'cmd')",
            }])
        with self.assertRaisesRegex(AppFoundryError, "call to 'getattr'"):
            self.foundry.create("Bad App", "", "main.py", [{
                "path": "main.py", "content": "getattr(object(), 'danger')",
            }])
        with self.assertRaisesRegex(AppFoundryError, "dunder name"):
            self.foundry.create("Bad App", "", "main.py", [{
                "path": "main.py", "content": "print(__builtins__)",
            }])

    def test_rejects_invalid_json_and_syntax(self) -> None:
        with self.assertRaisesRegex(AppFoundryError, "invalid JSON"):
            self.foundry.create("Bad App", "", "main.py", [
                {"path": "main.py", "content": "x=1"},
                {"path": "data.json", "content": "{"},
            ])
        with self.assertRaisesRegex(AppFoundryError, "syntax error"):
            self.foundry.create("Bad App", "", "main.py", [{"path": "main.py", "content": "if:"}])

    def test_launch_rechecks_integrity_and_uses_isolation(self) -> None:
        self._create()
        launched = self.foundry.launch("focus timer")
        self.assertIn("integrity=verified", launched)
        self.assertIn("pid=", launched)

        _, version_dir, manifest = self.foundry._current("focus timer")
        (version_dir / manifest["entrypoint"]).write_text("value = 9\n", encoding="utf-8")
        self.foundry._live["focus-timer"].terminate()
        self.foundry._live["focus-timer"].wait(timeout=3)
        with self.assertRaisesRegex(AppFoundryError, "integrity check failed"):
            self.foundry.launch("focus timer")

    def test_clean_environment_removes_credentials(self) -> None:
        os.environ["APP_FOUNDRY_TEST_API_KEY"] = "never-pass-this"
        try:
            clean = self.foundry._clean_env()
        finally:
            del os.environ["APP_FOUNDRY_TEST_API_KEY"]
        self.assertNotIn("APP_FOUNDRY_TEST_API_KEY", clean)
        self.assertEqual(clean["PYTHONNOUSERSITE"], "1")


if __name__ == "__main__":
    unittest.main()
