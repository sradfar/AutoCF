from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
sys.path.insert(0, str(ROOT / "app"))

from autocf_desktop_v2 import AutoCFWindow  # noqa: E402
from sfincs_agent import _read_user_point_forcing  # noqa: E402


class CanonicalWorkspaceTests(unittest.TestCase):
    def test_overlapping_project_and_event_are_not_duplicated(self) -> None:
        self.assertEqual(
            AutoCFWindow.event_workspace_stem("UK_Desmond", "", "Desmond_2015"),
            "UK_Desmond_2015",
        )

    def test_changed_event_replaces_prior_event_token(self) -> None:
        self.assertEqual(
            AutoCFWindow.event_workspace_stem(
                "UK_Desmond_2015", "Desmond_2015", "Ciara_2020"
            ),
            "UK_Ciara_2020",
        )

    def test_first_study_file_is_below_canonical_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = SimpleNamespace(
                geojson_path=SimpleNamespace(text=lambda: ""),
                session_mode="new",
                current_workspace_path=lambda: None,
                canonical_workspace_stem=lambda: "UK_Desmond_2015",
                project_base_dir=lambda: Path(directory),
            )
            result = AutoCFWindow.study_geojson_default(fake)
            self.assertEqual(
                result,
                Path(directory) / "UK_Desmond_2015" / "UK_Desmond_2015_study_area.geojson",
            )


class FileForcingTests(unittest.TestCase):
    def test_wide_river_csv_converts_cfs_to_metric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "river.csv"
            path.write_text(
                "time,riv001\n2020-01-01T00:00:00Z,35.3146667\n"
                "2020-01-01T01:00:00Z,70.6293334\n",
                encoding="utf-8",
            )
            frame = _read_user_point_forcing(
                path, {"input_layout": "wide", "units": "ft3/s", "time_zone": "UTC"}, "river"
            )
            self.assertAlmostEqual(float(frame.iloc[0, 0]), 1.0, places=6)
            self.assertAlmostEqual(float(frame.iloc[1, 0]), 2.0, places=6)

    def test_long_ocean_csv_pivots_and_applies_datum_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ocean.csv"
            path.write_text(
                "time,station_id,water_level\n"
                "2020-01-01T00:00:00Z,bnd001,100\n"
                "2020-01-01T01:00:00Z,bnd001,200\n",
                encoding="utf-8",
            )
            frame = _read_user_point_forcing(
                path,
                {
                    "input_layout": "long",
                    "units": "cm",
                    "time_zone": "UTC",
                    "datum_policy": "offset",
                    "source_vertical_datum": "Local",
                    "datum": "ODN",
                    "vertical_datum_offset_m": -0.5,
                },
                "ocean",
            )
            self.assertEqual(list(frame.columns), ["bnd001"])
            self.assertAlmostEqual(float(frame.iloc[0, 0]), 0.5)
            self.assertAlmostEqual(float(frame.iloc[1, 0]), 1.5)

    def test_unknown_ocean_datum_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ocean.csv"
            path.write_text(
                "time,bnd001\n2020-01-01T00:00:00Z,1.0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "datum is unknown"):
                _read_user_point_forcing(
                    path,
                    {"input_layout": "wide", "units": "m", "time_zone": "UTC",
                     "datum_policy": "unknown"},
                    "ocean",
                )


if __name__ == "__main__":
    unittest.main()
