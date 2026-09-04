from pathlib import Path
import sys

PREFIX = Path(sys.prefix)

def patch_file(path: Path, replacements):
    txt = path.read_text()
    original = txt
    for old, new, label in replacements:
        count = txt.count(old)
        if count == 0 and new in txt:
            print(f"[already patched] {label}")
            continue
        if count != 1:
            raise SystemExit(
                f"Patch safety failure for {label}: expected 1 occurrence of target, found {count}"
            )
        txt = txt.replace(old, new, 1)
        print(f"[patched] {label}")
    if txt != original:
        path.write_text(txt)

sfincs_py = PREFIX / "lib/python3.11/site-packages/hydromt_sfincs/sfincs.py"
vector_py = PREFIX / "lib/python3.11/site-packages/hydromt/vector.py"

patch_file(
    sfincs_py,
    [
        (
            "            if not (gdf_locs.index) == set(df_ts.columns):",
            "            if not set(gdf_locs.index) == set(df_ts.columns):",
            "HydroMT-SFINCS set_forcing_1d index comparison",
        ),
    ],
)

patch_file(
    vector_py,
    [
        (
            "        da = da.assign_coords({hdr: (index_dim, gdf.loc[_index, hdr]) for hdr in hdrs})",
            "        da = da.assign_coords({hdr: (index_dim, np.asarray(gdf.loc[_index, hdr], dtype=object) if hdr == geom_name else np.asarray(gdf.loc[_index, hdr])) for hdr in hdrs})",
            "HydroMT GeoDataArray geometry coordinate conversion",
        ),
        (
            "                    ds = ds.assign_coords({hdr: (index_dim, gdf.loc[_index, hdr])})",
            "                    ds = ds.assign_coords({hdr: (index_dim, np.asarray(gdf.loc[_index, hdr], dtype=object))})",
            "HydroMT GeoDataset geometry coordinate conversion (data-var mode)",
        ),
        (
            "            ds = ds.assign_coords(\n                {hdr: (index_dim, gdf.loc[_index, hdr]) for hdr in hdrs}\n            )",
            "            ds = ds.assign_coords(\n                {hdr: (index_dim, np.asarray(gdf.loc[_index, hdr], dtype=object) if hdr == geom_name else np.asarray(gdf.loc[_index, hdr])) for hdr in hdrs}\n            )",
            "HydroMT GeoDataset geometry coordinate conversion",
        ),
    ],
)

print("Compatibility patches: PASS")
