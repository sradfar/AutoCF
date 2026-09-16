from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleasePackagingTests(unittest.TestCase):
    def test_required_build_files_exist(self) -> None:
        for relative in (
            ".github/workflows/build-release.yml",
            "packaging/autocf_desktop.spec",
            "packaging/autocf_runtime_hook.py",
            "packaging/build_windows.ps1",
            "packaging/build_linux.sh",
            "packaging/build_macos.sh",
            "packaging/build_docker.sh",
            "packaging/macos-entitlements.plist",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_spec_contains_qt_webengine_and_native_targets(self) -> None:
        source = (ROOT / "packaging/autocf_desktop.spec").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("PySide6.QtWebEngineCore", source)
        self.assertIn("qtwebengineprocess", source.lower())
        self.assertIn("BUNDLE(", source)
        self.assertIn("COLLECT(", source)

    def test_macos_builder_signs_and_can_notarize(self) -> None:
        source = (ROOT / "packaging/build_macos.sh").read_text(encoding="utf-8")
        self.assertIn("codesign --verify", source)
        self.assertIn("notarytool submit", source)
        self.assertIn("hdiutil create", source)

    def test_workflow_uses_target_os_runners(self) -> None:
        source = (ROOT / ".github/workflows/build-release.yml").read_text(encoding="utf-8")
        for label in ("windows-2025", "ubuntu-24.04", "macos-15", "macos-15-intel"):
            self.assertIn(label, source)


if __name__ == "__main__":
    unittest.main()
