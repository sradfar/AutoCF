import os
import sys
from pathlib import Path

import pandas as pd
import numpy as np
import hydromt
import hydromt_sfincs
import geopandas as gpd
from shapely.geometry import Point
from hydromt.vector import GeoDataArray, GeoDataset
import xarray as xr

print("python:", sys.version)
print("pandas:", pd.__version__)
print("hydromt:", hydromt.__version__)
print("hydromt_sfincs:", hydromt_sfincs.__version__)

assert pd.__version__ == "2.2.3"
assert hydromt.__version__ == "0.9.4"
assert hydromt_sfincs.__version__ == "1.0.3"
assert os.environ.get("PYTHONNOUSERSITE") == "1"
assert "/.local/" not in "\n".join(sys.path)

# Test the exact Pandas API HydroMT-SFINCS 1.0.3 expects.
assert pd.RangeIndex(3).is_integer()
assert pd.Index([0.0, 1.0]).is_numeric()

# Test the exact GeoDataArray conversion that failed during the UAH port.
idx = pd.Index([1, 2, 3], name="index")
gdf = gpd.GeoDataFrame(
    {"ID": [1, 2, 3]},
    index=idx,
    geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
    crs=4326,
)
times = pd.date_range("2024-01-01", periods=4, freq="h", name="time")
df = pd.DataFrame(
    np.arange(12, dtype=float).reshape(4, 3),
    index=times,
    columns=idx,
)
df.columns.name = "index"

da = GeoDataArray.from_gdf(gdf, data=df, name="bzs")
assert da.shape == (3, 4)

# Test GeoDataset conversion too, because SfincsModel.read_forcing() uses it.
ds0 = xr.Dataset(
    {"bzs": (("index", "time"), np.arange(12, dtype=float).reshape(3, 4))},
    coords={"index": idx, "time": times},
)
ds = GeoDataset.from_gdf(gdf, ds0, index_dim="index")
assert "geometry" in ds.coords

print("HydroMT vector compatibility: PASS")
print("Runtime compatibility: PASS")
