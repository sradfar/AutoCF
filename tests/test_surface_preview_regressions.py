from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
import yaml
from rasterio.transform import from_bounds


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
sys.path.insert(0, str(ROOT / "app"))

from autocf_runtime import (  # noqa: E402
    _surface_rasters,
    invalidate_surface_previews,
    write_surface_preview,
)
from sfincs_agent import geoseries_union_all  # noqa: E402


DOMAIN = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {"role": "domain"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        },
    }],
}


class _LegacyGeoSeries:
    unary_union = "legacy-union"


class _ModernGeoSeries:
    unary_union = "wrong-fallback"

    def union_all(self):
        return "modern-union"


class SurfacePreviewRegressionTests(unittest.TestCase):
    def test_geoseries_union_supports_packaged_and_new_geopandas(self) -> None:
        self.assertEqual(geoseries_union_all(_LegacyGeoSeries()), "legacy-union")
        self.assertEqual(geoseries_union_all(_ModernGeoSeries()), "modern-union")

    def test_subgrid_preview_does_not_select_stale_regular_raster(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            surface = root / "data" / "surface" / "rasters"
            vectors = root / "data" / "geospatial" / "vectors"
            surface.mkdir(parents=True)
            vectors.mkdir(parents=True)
            (vectors / "domain.geojson").write_text(json.dumps(DOMAIN), encoding="utf-8")
            regular = surface / "esa_worldcover_model_crs.tif"
            subgrid = surface / "esa_worldcover_model_crs_subgrid_10m.tif"
            regular.touch()
            subgrid.touch()
            config = root / "model.yml"
            config.write_text(yaml.safe_dump({
                "project": {"output_dir": str(root), "layout_version": 2},
                "model": {"mode": "subgrid"},
                "subgrid": {"landcover_output_resolution_m": 10},
                "landcover": {"source": "ESA_WorldCover_2021_v200"},
                "infiltration": {"enabled": False},
            }), encoding="utf-8")

            _, _, specs = _surface_rasters(config)
            self.assertEqual(Path(specs[0]["path"]), subgrid)

    def test_failed_surface_run_invalidates_old_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "model.yml"
            config.write_text(yaml.safe_dump({
                "project": {"output_dir": str(root), "layout_version": 2},
            }), encoding="utf-8")
            manifest = root / "data" / "previews" / "surface" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"layers": [{"key": "stale"}]}), encoding="utf-8")

            invalidate_surface_previews(config, "failed")

            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(document["layers"], [])
            self.assertEqual(document["status"], "failed")

    def test_curve_number_coverage_excludes_permanent_water(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            transform = from_bounds(0, 0, 2, 2, 2, 2)
            profile = {
                "driver": "GTiff", "width": 2, "height": 2, "count": 1,
                "crs": "EPSG:4326", "transform": transform, "dtype": "uint8",
            }
            cn = root / "cn.tif"
            with rasterio.open(cn, "w", nodata=255, **profile) as dst:
                dst.write(np.array([[75, 255], [80, 255]], dtype="uint8"), 1)
            landcover = root / "landcover.tif"
            with rasterio.open(landcover, "w", nodata=0, **profile) as dst:
                dst.write(np.array([[40, 80], [40, 80]], dtype="uint8"), 1)
            domain = root / "domain.geojson"
            domain.write_text(json.dumps(DOMAIN), encoding="utf-8")

            result = write_surface_preview({
                "key": "curve_number", "label": "SCS curve number",
                "path": cn, "style": "cn", "coverage_mask": landcover,
                "coverage_exclude_values": [0, 80],
                "coverage_basis": "non-water land",
            }, root, domain)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertAlmostEqual(result["coverage_fraction"], 1.0, places=2)
            self.assertAlmostEqual(result["aoi_coverage_fraction"], 0.5, places=2)
            self.assertEqual(result["coverage_basis"], "non-water land")


if __name__ == "__main__":
    unittest.main()
