# AutoCF cross-HPC collaboration

This repository is prepared for development on more than one university HPC.

## Reproducibility model

**Authoritative environment:** `environment/explicit-linux-64.txt`, generated
from the already working Princeton conda environment with
`tools/freeze_environment.py`.

This exact file records conda package versions, build strings, source URLs,
and MD5 checksums. It is preferred over manually maintaining package versions.

Two collaborator execution modes are supported:

1. `tools/create_exact_env.sh` recreates the exact Linux conda environment.
2. `environment/Apptainer.def` builds an environment-only `.sif` from the same
   exact lock. Source code stays outside the image, so Python edits do not
   require rebuilding the container.

`environment/environment.yml` is only a portable fallback for a new clean
environment; it is not the reference lock.

## SFINCS executable

The SFINCS executable is external to the Python environment. The freeze tool
records its SHA256 checksum and dynamic-library information. The collaborator
should use the same SFINCS release/binary when possible and set:

```bash
export SFINCS_EXECUTABLE=/path/to/sfincs
```

For GPU SFINCS inside Apptainer, set `AUTOCF_USE_GPU=1`; the wrapper passes
`--nv`.

## Portable cases

Use:

```text
configs/sfincs_agent_config.portable.yml
configs/sfincs_agent_subgrid_config.portable.yml
```

and define site paths with:

```bash
export AUTOCF_WORKSPACE=/path/to/project/scratch
export AUTOCF_GEOMETRY=/path/to/domain.geojson
export SFINCS_EXECUTABLE=/path/to/sfincs
```

The Princeton Tampa/Helene configs are retained under `configs/` only as
reproducibility examples.

## Environment validation

```bash
python tools/doctor.py --config your_case.yml
python tests/smoke_test.py
```

`doctor.py` checks core Python/geospatial packages, HydroMT-SFINCS,
required GDAL executables, and the SFINCS binary/checksum.
