from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
from autocf_desktop_v2 import AutoCFWindow, role_feature
from map_export import coordinate_label, export_html, frame_image
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QImage


def point(role, identifier, lon=-2.8, lat=54.0):
    return role_feature(role, {"type": "Point", "coordinates": [lon, lat]}, {"ID": identifier})


def fixture():
    return {"type": "FeatureCollection", "name": "keep metadata", "features": [
        role_feature("domain", {"type": "Polygon", "coordinates": [[[-3,53],[-2,53],[-2,55],[-3,55],[-3,53]]]}, {}),
        role_feature("ocean_boundary", {"type": "LineString", "coordinates": [[-3,53],[-3,55]]}, {}),
        point("observation_point", "obs001"), point("ocean_forcing_point", "bnd001"),
        point("river_source", "1")]}


class Changes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_merge_preserves_other_roles_and_metadata(self):
        saved = fixture(); draft = fixture()
        draft["features"][0]["properties"]["unsaved_domain_edit"] = True
        draft["features"][-1] = point("river_source", "2", -2.7)
        result = AutoCFWindow.merge_forcing_points(saved, draft)
        self.assertEqual(result["features"][:3], saved["features"][:3])
        self.assertEqual(result["name"], "keep metadata")
        self.assertEqual(result["features"][-1]["properties"]["ID"], "2")

    def test_delete_last_point_is_saved(self):
        draft = fixture(); draft["features"].pop()
        result = AutoCFWindow.merge_forcing_points(fixture(), draft)
        self.assertFalse(any(f["properties"]["role"] == "river_source" for f in result["features"]))

    def test_invalid_coordinates_and_duplicate_ids_rejected(self):
        for bad in (point("river_source", "1"), point("river_source", "2", float("nan")),
                    point("river_source", "2", 181), point("river_source", "2", lat=91)):
            with self.subTest(bad=bad):
                draft = fixture(); draft["features"].append(bad)
                with self.assertRaises(ValueError):
                    AutoCFWindow.merge_forcing_points(fixture(), draft)

    def test_atomic_write_and_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "study.geojson"
            AutoCFWindow.atomic_write_text(p, json.dumps(fixture()))
            self.assertEqual(json.loads(p.read_text()), fixture())
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_yaml_failure_restores_geojson_and_blocks_success(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "study.geojson"; original = json.dumps(fixture())
            p.write_text(original)
            draft = fixture(); draft["features"].pop()
            fake = SimpleNamespace(
                geojson_path=SimpleNamespace(text=lambda: str(p), setText=lambda _: None),
                user_path=Path, merge_forcing_points=AutoCFWindow.merge_forcing_points,
                atomic_write_text=AutoCFWindow.atomic_write_text, mark_dirty=lambda: None,
                save_config=lambda: False,
                ocean_file_status=SimpleNamespace(setText=lambda _: None),
                river_file_status=SimpleNamespace(setText=lambda _: None))
            with patch("autocf_desktop_v2.QMessageBox.warning"):
                self.assertFalse(AutoCFWindow._save_forcing_point_payload(fake, json.dumps(draft)))
            self.assertEqual(p.read_text(), original)
            self.assertTrue(fake._forcing_save_error)
            self.assertFalse(fake._forcing_save_pending)

    def test_framing_keeps_map_pixels_and_sets_300dpi(self):
        image = QImage(900, 600, QImage.Format.Format_RGB32); image.fill(QColor("#123456"))
        ticks = {side: [[.5, value]] for side, value in
                 [("top", -2.8), ("bottom", -2.8), ("left", 54.0), ("right", 54.0)]}
        result = frame_image(image, ticks)
        self.assertEqual((result.width(), result.height()), (1152, 852))
        self.assertAlmostEqual(result.dotsPerMeterX() * .0254, 300, places=1)
        self.assertEqual(result.pixelColor(576, 426), QColor("#123456"))
        self.assertEqual(coordinate_label(181, True), "179.0000°W")

    def test_export_escapes_script_termination(self):
        page = export_html({"bad": "</script>"})
        self.assertIn("\\u003c/script>", page)


if __name__ == "__main__":
    unittest.main()
