from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class MacOSSanitizedReleaseTests(unittest.TestCase):
    def test_assembler_produces_source_free_payload(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "packaging"))
        import prepare_macos_sanitized as prepare
        from verify_macos_sanitized import audit_directory

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "source"
            payload = base / "payload"
            for directory in ("app", "vendor", "data", "examples", "logo", "windows/runtime"):
                (source / directory).mkdir(parents=True, exist_ok=True)
            (source / "app" / "sfincs_agent.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / "app" / "sfincs_eval.py").write_text("VALUE = 2\n", encoding="utf-8")
            (source / "vendor" / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
            (source / "windows" / "autocf_runtime.py").write_text("VALUE = 4\n", encoding="utf-8")
            (source / "windows" / "transformez_helper.py").write_text("VALUE = 5\n", encoding="utf-8")
            (source / "windows" / "riva_qa_database.json").write_text("{}\n", encoding="utf-8")
            for name in (
                "autocf-wsl-rootfs.tar.gz", "sfincs-cpu-rootfs.tar.gz",
                "sfincs-cpu-launcher.sh",
            ):
                (source / "windows" / "runtime" / name).write_bytes(b"fixture")
            for name in ("README.md", "RELEASE-NOTES.txt", "START-HERE.txt"):
                (source / name).write_text("fixture\n", encoding="utf-8")

            with patch.object(prepare, "EXPECTED_CACHE_TAG", sys.implementation.cache_tag):
                prepare.assemble(source, payload, "1.0.0-rc3", "test")
            result = audit_directory(payload)
            self.assertEqual(result["errors"], [])
            self.assertFalse(any(payload.rglob("*.py")))

    def test_host_runtime_prefers_bytecode_with_source_fallback(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "windows"))
        from host_runtime import (
            container_engine_candidates, packaged_python_entry, shared_runtime_root,
        )

        candidates = container_engine_candidates("darwin", Path("/Users/researcher"))
        self.assertIn(
            Path("/Applications/Docker.app/Contents/Resources/bin/docker"),
            candidates,
        )
        self.assertIn(Path("/Users/researcher/.docker/bin/docker"), candidates)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "app" / "sfincs_agent.pyc"
            entry.parent.mkdir(parents=True)
            entry.write_bytes(b"bytecode")
            self.assertEqual(
                packaged_python_entry(root, "app/sfincs_agent"),
                "/workspace/app/sfincs_agent.pyc",
            )

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "mounted-app" / "autocf"
            source.mkdir(parents=True)
            (source / "release-manifest.json").write_text('{"version":"test"}\n')
            (source / "payload.bin").write_bytes(b"runtime")
            home = base / "Users" / "researcher"
            staged = shared_runtime_root(
                source, create=True, system="darwin", home=home)
            self.assertTrue(staged.is_relative_to(home))
            self.assertEqual((staged / "payload.bin").read_bytes(), b"runtime")

    def test_builder_and_spec_use_sanitized_payload(self) -> None:
        builder = (ROOT / "packaging" / "build_macos_rc3.sh").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "autocf_desktop.spec").read_text(encoding="utf-8")
        requirements = (ROOT / "packaging" / "requirements-build.txt").read_text(encoding="utf-8")
        runtime_hook = (ROOT / "packaging" / "autocf_runtime_hook.py").read_text(encoding="utf-8")
        sanitizer = (ROOT / "packaging" / "prepare_macos_sanitized.py").read_text(encoding="utf-8")
        agent = (ROOT / "app" / "sfincs_agent.py").read_text(encoding="utf-8")
        self.assertIn("prepare_macos_sanitized.py", builder)
        self.assertIn("verify_macos_sanitized.py", builder)
        self.assertIn("AUTOCF_PAYLOAD_ROOT", spec)
        self.assertIn("ruamel.yaml", requirements)
        self.assertIn("certifi", requirements)
        self.assertIn('"ruamel.yaml"', spec)
        self.assertIn('"_ruamel_yaml"', spec)
        self.assertIn('collect_data_files("certifi")', spec)
        self.assertIn("from ruamel.yaml import YAML", builder)
        self.assertIn("certifi.where()", builder)
        self.assertIn("certifi/cacert.pem", builder)
        self.assertIn('os.environ["SSL_CERT_FILE"]', runtime_hook)
        self.assertIn("certifi.where()", runtime_hook)
        self.assertIn("transformez_helper.pyc", sanitizer)
        self.assertIn("/opt/transformez-site", sanitizer)
        self.assertIn("transformez-helper.sh", sanitizer)
        self.assertIn("rootfs-unpacker", sanitizer)
        self.assertIn("TRANSFORMEZ_VERSION=0.2.3", sanitizer)
        self.assertNotIn("ADD windows/runtime/autocf-wsl-rootfs.tar.gz", sanitizer)
        self.assertNotIn("ADD windows/runtime/sfincs-cpu-rootfs.tar.gz", sanitizer)
        self.assertIn("exact-calendar-days-v2", agent)
        self.assertIn("filter_observation_locations_to_domain", agent)


if __name__ == "__main__":
    unittest.main()
