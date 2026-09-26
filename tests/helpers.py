"""Shared test helpers."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from hrimgr import children, names
from hrimgr.registry import Registry

from . import ROOT

FIXTURE_CONFIG = ROOT / "tests" / "fixtures" / "hri_v0.25.0" / "app_config.yaml"
# HRI 0.25.2's template (feat/0.25.2 at ef3a7ef): backup_pre / backup_post, and its own backups kept in a HA backup
FIXTURE_0252 = ROOT / "tests" / "fixtures" / "hri_v0.25.2"


def tmpdir(test) -> str:
    path = tempfile.mkdtemp(prefix="hri-mgr-test-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


def instance_id(name: str) -> str:
    return (name.encode().hex() + "0" * 32)[:32]


def marker(name: str, **over) -> dict:
    data = {"manager": children.MANAGER_ID, "name": name, "slug": names.supervisor_slug(name), "channel": "release",
            "version": "0.25.0", "ref_kind": "tag", "ref": "v0.25.0", "sha": "a" * 40, "instance_id": instance_id(name),
            "template_source": "https://codeload.github.com/trailro/hass-remote-integration/tar.gz/refs/tags/v0.25.0",
            "history": []}
    data.update(over)
    return data


def new_registry(test) -> Registry:
    """A registry of its own, outside any local apps folder (the manager's /data)."""
    return Registry(os.path.join(tmpdir(test), "instances.json"))


def register(registry: Registry, data: dict) -> None:
    registry.put(data["name"], {k: data.get(k) for k in ("name", "slug", "channel", "version", "ref_kind", "ref", "sha",
                                                         "instance_id")} | {"setup_complete": True})


def make_child(root: str, name: str, marker_data: dict | None = None, config: bytes = b"slug: x\n",
               registry: Registry | None = None) -> str:
    """An instance folder with a marker (``marker_data``: False for none), in the registry when one is given."""
    folder = os.path.join(root, names.folder_name(name))
    os.makedirs(folder)
    with open(os.path.join(folder, "config.yaml"), "wb") as fh:
        fh.write(config)
    if marker_data is not False:
        data = marker(name) if marker_data is None else marker_data
        with open(os.path.join(folder, children.MARKER), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        if registry is not None and isinstance(data, dict) and data.get("name") == name:
            register(registry, data)
    return folder
