"""Shared test helpers."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from hrimgr import children, names

from . import ROOT

FIXTURE_CONFIG = ROOT / "tests" / "fixtures" / "hri_v0.25.0" / "app_config.yaml"


def tmpdir(test) -> str:
    path = tempfile.mkdtemp(prefix="hri-mgr-test-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


def marker(name: str, **over) -> dict:
    data = {"manager": children.MANAGER_ID, "name": name, "slug": names.supervisor_slug(name), "channel": "release",
            "version": "0.25.0", "ref": "v0.25.0", "sha": "a" * 40, "history": []}
    data.update(over)
    return data


def make_child(root: str, name: str, marker_data: dict | None = None, config: bytes = b"slug: x\n") -> str:
    folder = os.path.join(root, names.folder_name(name))
    os.makedirs(folder)
    with open(os.path.join(folder, "config.yaml"), "wb") as fh:
        fh.write(config)
    if marker_data is not False:
        with open(os.path.join(folder, children.MARKER), "w", encoding="utf-8") as fh:
            json.dump(marker(name) if marker_data is None else marker_data, fh)
    return folder
