# astrostack — reconstruction worker image for AWS Batch.
#
# Two-stage build with a single, deliberate property: **the runtime image has
# no GPU dependency at all.** Rule 5 of the astro-ml brief is that every
# pipeline runs without AWS and without a GPU; the GPU accelerates Tier B, it
# does not enable anything. So the default target is a plain Debian slim image
# that runs the whole Tier A pipeline on CPU, and the CUDA layer is an
# *optional* target on top of it for Tier B inference and training.
#
#   # CPU (default) — Tier A, ~1.2 GB
#   docker build -t astrostack:cpu models/
#
#   # GPU — Tier A plus Tier B with torch/CUDA
#   docker build --target gpu -t astrostack:gpu models/
#
#   docker run --rm -v /data:/data astrostack:cpu \
#       run /opt/astrostack/configs/classical-stack-v1.yaml \
#       --inputs /data/manifest.json --out /data/out
#
# The image deliberately does NOT bundle astrometry.net index files (they are
# 2-40 GB depending on the field scale) or the ASTAP star databases. Mount them
# and point the solver at them, or run with `solver: noop` on already-solved
# FITS. `astrostack validate` will tell you which stages a config needs.

# ---------------------------------------------------------------------------
# builder: compile wheels once, so the runtime layers stay free of toolchains
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        pkg-config \
        libraw-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY astrostack ./astrostack
COPY training ./training
COPY configs ./configs

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel && pip install .

# ---------------------------------------------------------------------------
# runtime (default): CPU only, Tier A complete
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS cpu

LABEL org.opencontainers.image.title="astrostack" \
      org.opencontainers.image.description="Multi-observer astronomical image reconstruction (Tier A)" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1
# Thread limits are set to 1 on purpose: AWS Batch packs several jobs per
# instance and oversubscribed BLAS threads make every job slower. The runner
# parallelises across frames, not inside BLAS.

RUN apt-get update && apt-get install -y --no-install-recommends \
        libraw20 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 astro

COPY --from=builder /opt/venv /opt/venv
COPY --chown=astro:astro configs /opt/astrostack/configs

# Baked in at build time so provenance.json records the exact code that ran
# even though the image has no .git directory.
ARG GIT_SHA=unknown
ENV ASTROSTACK_GIT_SHA=${GIT_SHA}

USER astro
WORKDIR /data

# Fail the build if the package cannot even start, and prove the CPU image
# genuinely does not need torch.
RUN astrostack --help > /dev/null \
    && astrostack validate /opt/astrostack/configs/classical-stack-v1.yaml > /dev/null \
    && python -c "import sys, astrostack.cli; assert 'torch' not in sys.modules"

ENTRYPOINT ["astrostack"]
CMD ["--help"]

# ---------------------------------------------------------------------------
# gpu (optional): everything above, plus torch/CUDA for Tier B
# ---------------------------------------------------------------------------
FROM nvidia/cuda:13.3.1-runtime-ubuntu22.04 AS gpu

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-venv \
        libraw20 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 astro

COPY --from=builder /opt/venv /opt/venv
COPY --chown=astro:astro configs /opt/astrostack/configs

# Tier B only. If this layer fails to build, the CPU image is still complete
# and still ships the scientific product.
RUN pip install --no-cache-dir "torch>=2.3" --index-url https://download.pytorch.org/whl/cu124

ARG GIT_SHA=unknown
ENV ASTROSTACK_GIT_SHA=${GIT_SHA}

USER astro
WORKDIR /data

RUN astrostack --help > /dev/null

ENTRYPOINT ["astrostack"]
CMD ["--help"]
