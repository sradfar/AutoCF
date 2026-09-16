"""Scientific runtime tests for ordered terrain preparation and actual HydroMT merge."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "vendor"), str(ROOT / "windows")]
import numpy as np
import rasterio
from rasterio.transform import from_origin
from sfincs_agent import (State, prepare_additional_topobathy, make_hydromt_build_config,
                          make_hydromt_catalog, read_yaml)
from vertical_datum import write_source_provenance_raster
from autocf_runtime import _resolve_config_file_paths, write_topobathy_preview


class AdditionalTerrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.transform = from_origin(500000, 6000000, 100, 100)
        self.rasters = []
        for i in range(4):
            data = np.full((16, 16), -9999, dtype="float32")
            data[:, :4 * (i+1)] = (i+1)*10
            path = self.root / f"source{i+1}.tif"
            with rasterio.open(path, "w", driver="GTiff", width=16, height=16, count=1,
                               crs="EPSG:32630", transform=self.transform, dtype="float32", nodata=-9999) as dst:
                dst.write(data, 1)
            self.rasters.append(path)
        with rasterio.open(self.rasters[0]) as src:
            from rasterio.warp import transform_bounds
            self.bbox = list(transform_bounds(src.crs, "EPSG:4326", *src.bounds))
        self.paths = SimpleNamespace(work=self.root, out=self.root, cache=self.root,
                                     data_file=lambda section, name: self.root / name)
        self.state = State(self.root / "state.json")
        self.sections = [dict(source="file", path=str(p), vertical_datum="ODN", output_resolution_m=100)
                         for p in self.rasters[2:]]
        self.cfg = dict(topobathy=dict(target_vertical_datum="ODN", gapfill=dict(source="file"),
                                      additional_sources=self.sections), mesh=dict(dx_m=100, crs="EPSG:32630"),
                        model=dict(mode="regular"), simulation=dict(start="2015-12-03", end="2015-12-08"),
                        sfincs={}, infiltration=dict(enabled=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_four_source_provenance(self):
        target = self.root / "provenance.tif"
        report = write_source_provenance_raster(*self.rasters[:2], target,
                    additional_sources=list(zip([3,4], self.rasters[2:])))
        with rasterio.open(target) as src:
            expected = np.repeat(np.arange(1,5), 4)
            np.testing.assert_array_equal(src.read(1)[8], expected)
        self.assertEqual(report["counts"], {"0":0,"1":64,"2":64,"3":64,"4":64})

    def test_prepare_cache_reorder_remove_and_build(self):
        prepared = prepare_additional_topobathy(self.cfg, self.paths, self.state, {}, self.bbox,
                                                "EPSG:32630", self.rasters[0], "primary-test")
        self.assertEqual([v["index"] for v in prepared], [3,4])
        self.assertTrue(all(Path(v["path"]).is_file() for v in prepared))
        with patch("sfincs_agent.gdalwarp_remote", side_effect=AssertionError("unexpected redownload")):
            prepare_additional_topobathy(self.cfg, self.paths, self.state, {}, self.bbox,
                                         "EPSG:32630", self.rasters[0], "primary-test")
        self.state.data["done"]["hydromt_build"] = True
        self.cfg["topobathy"]["additional_sources"] = list(reversed(self.sections))
        reordered = prepare_additional_topobathy(self.cfg, self.paths, self.state, {}, self.bbox,
                                                 "EPSG:32630", self.rasters[0], "primary-test")
        self.assertFalse(self.state.data["done"]["hydromt_build"])
        self.assertEqual(reordered[0]["metadata"]["source"], "file")
        self.cfg["_additional_topobathy"] = prepared
        import yaml
        for mode in ("regular", "subgrid"):
            self.cfg["model"]["mode"] = mode
            build = make_hydromt_build_config(self.cfg, self.paths,
                       dict(domain_path="domain.geojson", ocean_path="ocean.geojson"),
                       self.root/"water.csv", self.root/"water.geojson", None, None, None, None, None, None)
            config = yaml.safe_load(build.read_text())
            self.assertEqual([v["elevtn"] for v in config["setup_dep"]["datasets_dep"]],
                             ["topobathy_primary_grid", "gebco_500m", "topobathy_03", "topobathy_04"])
            if mode == "subgrid":
                self.assertEqual(len(config["setup_subgrid"]["datasets_dep"]), 4)
        catalog = make_hydromt_catalog(self.cfg, self.paths, self.rasters[0], self.rasters[1],
                     self.rasters[0], self.root/"manning.csv", None, None, None)
        cat = yaml.safe_load(catalog.read_text())
        self.assertEqual(cat["topobathy_03"]["path"], prepared[0]["path"])
        self.cfg["topobathy"]["additional_sources"] = []
        self.state.data["done"]["hydromt_build"] = True
        self.assertEqual(prepare_additional_topobathy(self.cfg, self.paths, self.state, {}, self.bbox,
                          "EPSG:32630", self.rasters[0], "primary-test"), [])
        self.assertFalse(self.state.data["done"]["hydromt_build"])

    def test_actual_hydromt_four_source_merge(self):
        from hydromt_sfincs import SfincsModel
        model = SfincsModel(root=self.root/"model", mode="w")
        model.setup_grid(x0=500000, y0=5998400, dx=100, dy=100, nmax=16, mmax=16, rotation=0, epsg=32630)
        model.setup_dep(datasets_dep=[dict(elevtn=str(p), merge_method="first", reproj_method="nearest")
                                     for p in self.rasters], buffer_cells=0)
        actual = model.grid["dep"].values
        self.assertEqual(set(np.unique(actual)), {10,20,30,40})
        for value in (10,20,30,40):
            self.assertEqual(int(np.sum(actual == value)), 64)

    def test_runtime_relative_paths_and_preview(self):
        cfg = dict(topobathy=dict(additional_sources=[dict(source="file", path="./source3.tif")]))
        self.assertEqual(_resolve_config_file_paths(cfg, self.root)["topobathy"]["additional_sources"][0]["path"],
                         str(self.rasters[2]))
        config = self.root/"model.yml"
        config.write_text("topobathy:\n  additional_sources:\n    - source: file\n      path: ./source3.tif\n")
        self.assertEqual(read_yaml(config)["topobathy"]["additional_sources"][0]["path"], str(self.rasters[2]))
        report = write_topobathy_preview(self.rasters[0], self.rasters[1], "ODN",
                                         additional_sources=self.rasters[2:])
        self.assertEqual(len(report["sources"]), 4)
        self.assertGreater(report["coverage_fraction"], .95)


if __name__ == "__main__":
    unittest.main(verbosity=2)
