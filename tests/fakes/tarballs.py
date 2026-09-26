"""GitHub-like source tarballs built in memory: one top folder and a pax global header whose comment is the SHA."""

from __future__ import annotations

import hashlib
import io
import pathlib
import tarfile
import time

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "hri_v0.25.0"


def sha_of(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def make_tarball(top: str, files: dict[str, bytes], sha: str | None = None, extra=(), mtime: float | None = None) -> bytes:
    """``files``: relative path -> content.  ``extra``: TarInfo objects (with optional data) added as they are."""
    buf = io.BytesIO()
    pax = {"comment": sha} if sha else {}
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax) as tar:
        info = tarfile.TarInfo(top)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        for rel in sorted(files):
            data = files[rel]
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = time.time() if mtime is None else mtime
            tar.addfile(info, io.BytesIO(data))
        for item in extra:
            info, data = item if isinstance(item, tuple) else (item, None)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def hri_files(version_in_config: str = "0.24.0") -> dict[str, bytes]:
    """A small tree shaped like hass-remote-integration: its app/ template (the v0.25.0 fixture), a Dockerfile with
    the HRI_BUILD argument, the config.* files that are not apps (static/config.js...) and decoys that are."""
    config = (FIXTURES / "app_config.yaml").read_text(encoding="utf-8")
    config = config.replace('version: "0.24.0"', f'version: "{version_in_config}"')
    return {
        "app/config.yaml": config.encode(),
        "app/DOCS.md": (FIXTURES / "app_DOCS.md").read_bytes(),
        "app/CHANGELOG.md": (FIXTURES / "app_CHANGELOG.md").read_bytes(),
        "app/translations/en.yaml": (FIXTURES / "app_translations_en.yaml").read_bytes(),
        "Dockerfile": b"FROM python:3.14-slim\nARG HRI_BUILD=local\nENV HRI_BUILD=${HRI_BUILD}\n",
        "README.md": b"# hass-remote-integration\n",
        "repository.yaml": b"name: hass-remote-integration\n",
        "custom_components/integration_manager/static/config.js": b"// the Config page\n",
        "custom_components/integration_manager/static/config.css": b"/* the Config page */\n",
        "custom_components/integration_manager/templates/config.html": b"<!doctype html>\n",
        "tests/e2e/config.yaml": b"slug: decoy\nname: decoy\nversion: '1'\n",
        "tests/e2e/config.json": b"{}\n",
        ".github/haos/config.yaml": b"hidden: true\n",
    }
