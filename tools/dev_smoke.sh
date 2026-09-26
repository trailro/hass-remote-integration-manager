#!/bin/sh
# Smoke test of the app as a container, next to a fake Supervisor and a fake GitHub (tests/fakes/stub.py).
#
#   sh tools/dev_smoke.sh [screenshot folder]
#
# Builds hri_manager/ as the Supervisor would, then runs, on a throwaway network (198.51.100.0/24, a documentation
# range):
#   hri-mgr-dev-stub    the fake Supervisor + GitHub, sharing the local apps volume with the manager
#   hri-mgr-dev-mgr     the manager in development mode: its peer check accepts the driver's and the browser's
#                       addresses instead of the Supervisor's (HRI_MANAGER_DEV_*: variables the app cannot set)
#   hri-mgr-dev-driver  tools/dev_driver.py: create / update / rebuild / delete through the page's API
#   hri-mgr-dev-shots   tools/dev_screens.py in the Playwright image, when it is present locally
# The manager's port is published on 127.0.0.1 only, to show that a request from anywhere else is refused.
# Everything is removed at the end, also on failure.
set -eu

cd "$(dirname "$0")/.."
P=hri-mgr-dev
NET=$P-net
VOL=$P-local
IMG=$P:local
SHOTS=${1:-}
# Playwright 1.46's image, pinned by digest: its browsers (chromium-1129) are the ones playwright==1.46.0 drives, and
# it carries no Python package, so that one is installed in it.  Change both together
PLAYWRIGHT=${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright/python@sha256:98e88016a5705def757564f70469e80161c4e4dbf787bbe35a710fabb70bb7da}
PLAYWRIGHT_PIP=${PLAYWRIGHT_PIP:-1.46.0}
PORT=${HRI_MGR_DEV_PORT:-18099}

cleanup() {
    docker rm -f $P-stub $P-mgr $P-driver $P-shots >/dev/null 2>&1 || true
    docker network rm $NET >/dev/null 2>&1 || true
    docker volume rm $VOL >/dev/null 2>&1 || true
    docker image rm $IMG >/dev/null 2>&1 || true
}
trap cleanup EXIT
# an interrupted run stops after its cleanup (a bare INT or TERM trap would run the cleanup and go on)
trap 'cleanup; exit 130' INT TERM
cleanup

echo "== build"
case "$(docker info -f '{{.Architecture}}')" in aarch64|arm64) ARCH=aarch64 ;; *) ARCH=amd64 ;; esac
docker build --quiet --build-arg BUILD_ARCH=$ARCH --build-arg BUILD_VERSION=dev -t $IMG hri_manager >/dev/null
docker network create --subnet 198.51.100.0/24 $NET >/dev/null
docker volume create $VOL >/dev/null

echo "== fake Supervisor"
docker run -d --name $P-stub --network $NET --ip 198.51.100.10 -v "$PWD:/src:ro" -v $VOL:/local_apps -w /src \
    $IMG python -m tests.fakes.stub --local-apps /local_apps --host 0.0.0.0 --port 8080 --token dev-token --delay 1 >/dev/null

echo "== manager"
docker run -d --name $P-mgr --network $NET --ip 198.51.100.20 -p 127.0.0.1:$PORT:8099 -v $VOL:/local_apps \
    -e SUPERVISOR_TOKEN=dev-token \
    -e HRI_MANAGER_DEV_PEERS=198.51.100.30,198.51.100.31 \
    -e HRI_MANAGER_DEV_SUPERVISOR_URL=http://198.51.100.10:8080/sv \
    -e HRI_MANAGER_DEV_GITHUB_API=http://198.51.100.10:8080/gh \
    -e HRI_MANAGER_DEV_CODELOAD=http://198.51.100.10:8080/cl \
    -e HRI_MANAGER_DEV_DATA=/tmp \
    $IMG >/dev/null
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' $P-mgr)" = healthy ]; do
    i=$((i + 1)); [ $i -lt 60 ] || { docker logs $P-mgr; exit 1; }; sleep 1
done
echo "manager healthy"

code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)
echo "a request from the host (not a peer): HTTP $code"
[ "$code" = 403 ]

echo "== API"
docker run --rm --name $P-driver --network $NET --ip 198.51.100.30 -v "$PWD:/src:ro" $IMG \
    python /src/tools/dev_driver.py http://198.51.100.20:8099 http://198.51.100.10:8080

if [ -n "$SHOTS" ]; then
    if docker image inspect "$PLAYWRIGHT" >/dev/null 2>&1; then
        echo "== screenshots"
        mkdir -p "$SHOTS"
        docker run --rm --name $P-shots --network $NET --ip 198.51.100.31 -v "$PWD:/src:ro" -v "$SHOTS:/out" \
            "$PLAYWRIGHT" sh -c "python -c 'import playwright' 2>/dev/null || pip install --quiet --disable-pip-version-check playwright==$PLAYWRIGHT_PIP; python /src/tools/dev_screens.py http://198.51.100.20:8099 /out"
    else
        echo "== screenshots skipped: $PLAYWRIGHT is not present locally"
    fi
fi

echo "== manager log (warnings and errors)"
docker logs $P-mgr 2>&1 | grep -E "WARNING|ERROR" || echo "(none)"
if docker logs $P-mgr 2>&1 | grep -q "dev-token"; then echo "the token appears in the log" >&2; exit 1; fi
echo "== done"
