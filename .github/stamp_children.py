"""Write instances stamped from the HRI fixture, for the app linter: <out>/release and <out>/git.

    python .github/stamp_children.py <out>

Each folder is what the manager writes for an instance (config.yaml and HRI's translations), without the rest of a
git build's tree.  <out> must not be committed: its config.yaml files would be apps of this repository."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hri_manager"))

from hrimgr import names, stamp  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "hri_v0.25.0"


def main(out: pathlib.Path) -> int:
    template = stamp.parse_template((FIXTURE / "app_config.yaml").read_bytes())
    for channel, version in (("release", "0.25.0"), ("git", names.git_version("0123456789abcdef0123456789abcdef01234567"))):
        folder = out / channel
        (folder / "translations").mkdir(parents=True, exist_ok=False)
        (folder / "config.yaml").write_bytes(stamp.dump(stamp.stamp(template, "garage", version, channel), "the v0.25.0 fixture"))
        (folder / "translations" / "en.yaml").write_bytes((FIXTURE / "app_translations_en.yaml").read_bytes())
        (folder / "DOCS.md").write_bytes((FIXTURE / "app_DOCS.md").read_bytes())
        print(f"{folder}: {channel} {version}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
        sys.exit(2)
    sys.exit(main(pathlib.Path(sys.argv[1])))
