# syntax=docker/dockerfile:1.7

ARG NVHPC_VERSION=25.5
ARG SFINCS_REF=v2.4.0_Galibier_release

# -----------------------------------------------------------------------------
# 1) Deterministic Python / HydroMT environment
# -----------------------------------------------------------------------------
FROM mambaorg/micromamba:2.3.2 AS pyenv

COPY --chown=$MAMBA_USER:$MAMBA_USER container/environment.yml /tmp/environment.yml
RUN micromamba create -y -p /opt/autocf-env -f /tmp/environment.yml && \
    micromamba clean --all --yes

COPY --chown=$MAMBA_USER:$MAMBA_USER container/apply_compat_patches.py /tmp/apply_compat_patches.py
RUN micromamba run -p /opt/autocf-env python /tmp/apply_compat_patches.py && \
    micromamba run -p /opt/autocf-env python -m py_compile \
      /opt/autocf-env/lib/python3.11/site-packages/hydromt/vector.py \
      /opt/autocf-env/lib/python3.11/site-packages/hydromt_sfincs/sfincs.py

# -----------------------------------------------------------------------------
# 2) Compile SFINCS GPU/OpenACC once in a neutral NVIDIA HPC SDK image
# -----------------------------------------------------------------------------
FROM nvcr.io/nvidia/nvhpc:25.5-devel-cuda_multi-ubuntu22.04 AS sfincs-builder

ARG SFINCS_REF

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      autoconf automake libtool m4 make git pkg-config \
      libnetcdf-dev libnetcdff-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${SFINCS_REF}" \
      https://github.com/Deltares/SFINCS.git /src/SFINCS

WORKDIR /src/SFINCS/source
RUN autoreconf -vif && \
    CC=nvc FC=nvfortran ./configure --prefix=/opt/sfincs --disable-shared && \
    make -j"$(nproc)" && \
    make install

# Fail the image build if the expected GPU/OpenACC compiler flag did not get detected.
RUN grep -R -- '-acc' config.log src/Makefile Makefile >/dev/null

# -----------------------------------------------------------------------------
# 3) Final runtime: no compiler, no host Conda, no host Python packages
# -----------------------------------------------------------------------------
FROM nvcr.io/nvidia/nvhpc:25.5-runtime-cuda12.9-ubuntu22.04 AS runtime

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libnetcdf19 libnetcdff7 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=pyenv /opt/autocf-env /opt/autocf-env
COPY --from=sfincs-builder /opt/sfincs /opt/sfincs

WORKDIR /opt/autocf
COPY . /opt/autocf

COPY container/entrypoint.sh /usr/local/bin/autocf-entrypoint
RUN chmod 0755 /usr/local/bin/autocf-entrypoint && \
    ln -s /opt/autocf/autocf.py /usr/local/bin/autocf

ENV PATH=/opt/autocf-env/bin:/opt/sfincs/bin:$PATH \
    PYTHONNOUSERSITE=1 \
    HDF5_USE_FILE_LOCKING=FALSE \
    SFINCS_EXECUTABLE=/opt/sfincs/bin/sfincs \
    MPLBACKEND=Agg

ENTRYPOINT ["/usr/local/bin/autocf-entrypoint"]
CMD ["python", "/opt/autocf/doctor.py"]
