"""Can anyone pull the manager's image, for amd64 and arm64?  As a Supervisor pulls it: anonymously.

    python3 .github/check_published_image.py --config hri_manager/config.yaml   # CI: the image: line at its version
    python3 .github/check_published_image.py --ref ghcr.io/<owner>/<repo>:<version>   # the Image workflow

--config passes when the file has no image: line (the Supervisor builds the folder then: before the first release).
Exit 1 when the image cannot be pulled without credentials (missing, or a package still private) or lacks either
architecture: main would name an image the store offers and no Supervisor can install."""

import json
import os
import re
import subprocess
import sys
import tempfile

WANTED = {"linux/amd64", "linux/arm64"}


def from_config(path: str) -> str | None:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    image = re.search(r'^image: *"?([^"\n#]+?)"? *$', text, re.M)
    version = re.search(r'^version: *"?([^"\n#]+?)"? *$', text, re.M)
    if not image:
        return None
    if not version:
        sys.exit(f"{path} has an image: line but no version")
    return f"{image.group(1)}:{version.group(1)}"


def check(ref: str) -> int:
    with tempfile.TemporaryDirectory() as empty:
        env = {**os.environ, "DOCKER_CONFIG": empty}  # no credentials
        proc = subprocess.run(["docker", "manifest", "inspect", ref], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"::error::{ref} cannot be pulled anonymously ({proc.stderr.strip()[:200]}): missing, or a private "
              "package (make it public)")
        return 1
    try:
        found = {f"{m['platform']['os']}/{m['platform']['architecture']}"
                 for m in json.loads(proc.stdout).get("manifests", []) if "platform" in m}
    except (ValueError, KeyError, TypeError, AttributeError):
        found = set()
    print(f"{ref}: {', '.join(sorted(found)) or 'no platform list'}")
    missing = WANTED - found
    if missing:
        print(f"::error::{ref} has no {', '.join(sorted(missing))} image")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("--config", "--ref"):
        print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
        return 2
    ref = argv[2] if argv[1] == "--ref" else from_config(argv[2])
    if ref is None:
        print(f"{argv[2]} has no image line: the Supervisor builds the app on the device, nothing to pull")
        return 0
    return check(ref)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
