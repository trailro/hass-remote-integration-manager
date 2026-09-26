"""hri_manager/config.yaml's version and image: line, for the Image workflow's app-version job.

    gh release list --json tagName,isPrerelease,isDraft | python3 .github/app_version.py decide <tag> <image> <config>
    python3 .github/app_version.py apply <tag> <image> <config>

decide: says why, and writes move=true to $GITHUB_OUTPUT only when <tag> is the newest stable release (from the
release list, not from the event: a pre-release can carry a plain vX.Y.Z tag) and the file would change without its
version going down.  The job checks that the image can be pulled, and writes, only then: a pre-release, or a manual
re-run of an old tag, never fails on a package that is still private.

apply: sets version to <tag> without its v and, the first time, adds image: <image> under it; everything else stays
byte for byte.  tests/test_image_workflow.py runs the job's steps, which call this."""

import json
import os
import re
import sys

STABLE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
VERSION_LINE = re.compile(r'^version: "?([^"\n]*)"?$', re.M)
IMAGE_LINE = re.compile(r"^image: .*$", re.M)


def key(version: str) -> tuple:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(x) for x in m.groups()) if m else (-1,)


def newest_stable(releases: list) -> str:
    tags = [r["tagName"] for r in releases if not r.get("isPrerelease") and not r.get("isDraft")
            and STABLE_TAG.fullmatch(r.get("tagName", ""))]
    return max(tags, key=lambda t: key(t[1:])) if tags else ""


def state(path: str) -> tuple[str, str, bool]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return text, VERSION_LINE.search(text).group(1), IMAGE_LINE.search(text) is not None


def decide(tag: str, image: str, path: str, releases: list) -> tuple[bool, str]:
    newest = newest_stable(releases)
    if tag != newest:
        return False, f"tag {tag} is not the newest stable release ({newest or 'none'}): the app version stays as it is"
    version = tag[1:]
    _, current, has_image = state(path)
    if current == version and has_image:
        return False, f"{path} already names {version} and its image"
    if key(current) > key(version):
        return False, f"{path} names {current}, newer than {version}: the app version stays as it is"
    return True, f"{path}: version {current} -> {version}" + ("" if has_image else f", image {image}")


def apply(tag: str, image: str, path: str) -> str:
    version = tag[1:]
    text, current, has_image = state(path)
    line = VERSION_LINE.search(text)
    text = text[:line.start()] + f'version: "{version}"' + ("" if has_image else f"\nimage: {image}") + text[line.end():]
    if has_image:
        text = IMAGE_LINE.sub(f"image: {image}", text, count=1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return f"{path}: version {current} -> {version}" + ("" if has_image else f", image {image}")


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[1] not in ("decide", "apply"):
        print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
        return 2
    mode, tag, image, path = argv[1:]
    if mode == "apply":
        print(apply(tag, image, path))
        return 0
    move, why = decide(tag, image, path, json.load(sys.stdin))
    print(why)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write(f"move={'true' if move else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
