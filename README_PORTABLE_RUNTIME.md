# AutoCF portable NVIDIA runtime v1

This branch is infrastructure-only. The protected scientific baseline is
`autocf-portable-baseline-v1.1`.

## Protected source hashes

- `sfincs_attribution.py`  
  `917888ccf179855a2edf1e366331cff1bee32c1f7a13c8bbc72b6d5a8c9027f8`
- `sfincs_global_validation.py`  
  `8683b1331d4a7ebe01097a812f0573781ed5d0ea3e72d18e8450e8a08374fa6e`
- `sfincs_hydraulic_core.py`  
  `eed0c8d27fc1f29ae1388bc1febfde19f8960788bff122cacbdf2a58e42aacec`
- `autocf.py`  
  `887aaa68013b7d9ad0049f7a57daf458ebc1f3d6f1c4ef936062a1e05adec24a`

The GitHub workflow refuses to build when any protected hash changes.

## Runtime design

The OCI image is built once on GitHub and contains:

- Python 3.11
- fixed HydroMT / HydroMT-SFINCS stack
- pinned Pandas 2.2.3
- GDAL, NetCDF4, GeoPandas, Rasterio, Overture client, CDS API
- explicit compatibility patches required by HydroMT 0.9.4 / HydroMT-SFINCS 1.0.3
- SFINCS v2.4.0 Galibier compiled with NVIDIA HPC SDK/OpenACC

The final runtime is based on the NVIDIA HPC SDK runtime image, not on Della
or UAH software modules.

Host Python, Conda, GCC, NetCDF, GDAL and user-site packages are not used.

## Important release rule

`portable-v1-candidate` is **not** a release merely because GitHub built it.

The exact same candidate image must first pass:

```bash
apptainer exec --nv --cleanenv autocf-nvidia-v1-candidate.sif \
  bash /opt/autocf/tests/portable/gpu_acceptance.sh
```

on both:

1. Princeton Della
2. the collaborator's NVIDIA HPC

Only after both pass should the image be promoted to `portable-v1`.

## HPC execution

After a candidate is accepted, each HPC only needs Apptainer and a compatible
NVIDIA driver. No Conda environment or local SFINCS compilation is required.

Typical execution:

```bash
apptainer exec --nv --cleanenv \
  --env HDF5_USE_FILE_LOCKING=FALSE \
  --bind /path/to/workspace:/workspace \
  autocf-nvidia-v1.sif \
  python /opt/autocf/autocf.py --config /workspace/config.yml run
```

A site's scheduler command and filesystem paths remain site-specific; the
scientific software runtime does not.
