from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from sfincs_agent import (  # noqa: E402
    era5_calendar_request_groups,
    filter_observation_locations_to_domain,
)


class Era5CalendarRequestTests(unittest.TestCase):
    def test_cross_month_event_requests_only_exact_dates(self) -> None:
        dates = pd.date_range("2022-09-22", "2022-10-01", freq="D")
        groups = era5_calendar_request_groups(dates)
        self.assertEqual(groups, [
            {
                "year": ["2022"],
                "month": ["09"],
                "day": ["22", "23", "24", "25", "26", "27", "28", "29", "30"],
            },
            {"year": ["2022"], "month": ["10"], "day": ["01"]},
        ])
        expanded = [
            f"{group['year'][0]}-{group['month'][0]}-{day}"
            for group in groups for day in group["day"]
        ]
        self.assertEqual(expanded, [value.strftime("%Y-%m-%d") for value in dates])
        self.assertNotIn("2022-10-22", expanded)

    def test_cross_year_event_is_grouped_by_calendar_month(self) -> None:
        dates = pd.date_range("2022-12-31", "2023-01-02", freq="D")
        self.assertEqual(era5_calendar_request_groups(dates), [
            {"year": ["2022"], "month": ["12"], "day": ["31"]},
            {"year": ["2023"], "month": ["01"], "day": ["01", "02"]},
        ])


class ObservationLocationTests(unittest.TestCase):
    @staticmethod
    def _write_geojson(path: Path, geometry_type: str, coordinates) -> None:
        features = [
            {
                "type": "Feature",
                "properties": {"ID": index + 1},
                "geometry": {"type": geometry_type, "coordinates": geometry},
            }
            for index, geometry in enumerate(coordinates)
        ]
        path.write_text(json.dumps({
            "type": "FeatureCollection",
            "name": path.stem,
            "crs": {
                "type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
            },
            "features": features,
        }), encoding="utf-8")

    @staticmethod
    def _domain_coordinates():
        return [[
            [-83.0, 27.0], [-82.0, 27.0], [-82.0, 28.0],
            [-83.0, 28.0], [-83.0, 27.0],
        ]]

    def test_empty_observation_geojson_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = root / "observations.geojson"
            domain = root / "domain.geojson"
            self._write_geojson(observations, "Point", [])
            self._write_geojson(domain, "Polygon", [self._domain_coordinates()])
            self.assertIsNone(
                filter_observation_locations_to_domain(observations, domain)
            )

    def test_observations_outside_domain_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = root / "observations.geojson"
            domain = root / "domain.geojson"
            self._write_geojson(observations, "Point", [[-80.0, 25.0]])
            self._write_geojson(domain, "Polygon", [self._domain_coordinates()])
            self.assertIsNone(
                filter_observation_locations_to_domain(observations, domain)
            )

    def test_mixed_observations_are_filtered_in_model_order(self) -> None:
        import geopandas as gpd

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = root / "observations.geojson"
            domain = root / "domain.geojson"
            self._write_geojson(
                observations, "Point", [[-82.5, 27.5], [-80.0, 25.0]]
            )
            self._write_geojson(domain, "Polygon", [self._domain_coordinates()])
            self.assertEqual(
                filter_observation_locations_to_domain(observations, domain),
                observations,
            )
            filtered = gpd.read_file(observations)
            self.assertEqual(len(filtered), 1)
            self.assertEqual(int(filtered.iloc[0]["ID"]), 1)


if __name__ == "__main__":
    unittest.main()
