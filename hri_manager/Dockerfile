# pinned by digest (a multi-arch index: amd64 and arm64 among its platforms): a rebuild gets the same base;
# Dependabot proposes the updates.  Built for amd64 and arm64 by the Image workflow and pushed to ghcr.io; the
# Supervisor builds it on the device instead while config.yaml has no image: line, and passes BUILD_ARCH and
# BUILD_VERSION, which this file does not need
FROM python:3.14-alpine@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/hri_manager

# requirements.txt is the lock pip-compile makes of requirements.in: every dependency, transitive ones too, pinned
# with the hashes of its files, so every build installs what CI tested.  Wheels only: no compiler in the image, and a
# dependency without a wheel for this platform fails the build here instead of half-installing
COPY requirements.txt ./
RUN pip install --require-hashes --only-binary=:all: --no-deps -r requirements.txt

COPY hrimgr ./hrimgr

# liveness only, from inside the container: the manager answers /healthz on the loopback without the ingress check
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4)"]

CMD ["python", "-m", "hrimgr"]
