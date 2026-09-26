# pinned by digest: a rebuild gets the same base; Dependabot proposes the updates.  The Supervisor builds this on the
# device (no image: in config.yaml) and passes BUILD_ARCH and BUILD_VERSION, which this file does not need
FROM python:3.14-alpine@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/hri_manager

# wheels only: no compiler in the image, and a dependency without a wheel for this platform fails the build here
# instead of half-installing
COPY requirements.txt ./
RUN pip install --only-binary=:all: -r requirements.txt

COPY hrimgr ./hrimgr

# liveness only, from inside the container: the manager answers /healthz on the loopback without the ingress check
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4)"]

CMD ["python", "-m", "hrimgr"]
