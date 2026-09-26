"""An instance's definition: HRI's own app template, stamped.

HRI stays the source of truth.  The manager takes ``app/config.yaml`` (with DOCS.md, CHANGELOG.md, the translations
and the icons) from the HRI release or git ref and changes only what makes the copy a separate app:

- ``slug: hri_<name>``, ``name: HRI <Name>``, ``panel_title: HRI <name>``;
- ``version``: the release's own (at a release tag HRI's app/config.yaml still names the previous version until the
  Image workflow moves it), or ``0.0.0-<sha>`` for a git build;
- ``ports``: every port unpublished (``null``); the sidebar panel works through ingress, and a port can be mapped on
  the app's Network tab;
- ``backup_exclude``: the ``*_hass_remote_integration/`` prefix becomes ``*_hri_<name>/``, the folder of this
  instance, or its Home Assistant backups would hold the installed Home Assistant (about 800 MB);
- ``webui`` dropped; ``image`` dropped for a git build (the Supervisor builds the folder instead).

Everything else (options, schema, ingress, map, homeassistant, image, arch, timeout, uart...) is kept as HRI wrote
it.  The YAML is written with ``yaml.safe_dump``."""

from __future__ import annotations

import os
import re
from typing import Any

import yaml

from . import children, names, tarsafe

HRI_BACKUP_PREFIX = f"*_{names.HRI_SLUG}/"
STAMPED_KEYS = ("name", "version", "slug", "panel_title", "ports", "backup_exclude")
CONFIG_SUFFIXES = (".yaml", ".yml", ".json")  # what the Supervisor's store reads as config.* (FILE_SUFFIX_CONFIGURATION)
APP_FILES = ("DOCS.md", "CHANGELOG.md", "README.md", "icon.png", "logo.png")
# HRI's Dockerfile: the commit the image was built from.  The Supervisor passes only BUILD_VERSION and BUILD_ARCH
# to a build without build.yaml (deprecated), so a git build gets its commit as the argument's default instead
HRI_BUILD_ARG = re.compile(rb"^ARG HRI_BUILD=local$", re.M)
MAX_TEMPLATE = 256 * 1024


class TemplateError(Exception):
    pass


def parse_template(raw: bytes) -> dict:
    if len(raw) > MAX_TEMPLATE:
        raise TemplateError("HRI's app/config.yaml is larger than expected")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as err:
        raise TemplateError(f"HRI's app/config.yaml does not parse: {err}") from None
    if not isinstance(data, dict):
        raise TemplateError("HRI's app/config.yaml is not a mapping")
    missing = [k for k in STAMPED_KEYS if k not in data]
    if missing:
        raise TemplateError(f"HRI's app/config.yaml lacks {', '.join(missing)}: this manager cannot stamp it")
    if data.get("slug") != names.HRI_SLUG:
        raise TemplateError(f"HRI's app/config.yaml has slug {data.get('slug')!r}, not {names.HRI_SLUG!r}")
    if not isinstance(data.get("ports"), dict):
        raise TemplateError("HRI's app/config.yaml: ports is not a mapping")
    if not isinstance(data.get("backup_exclude"), list) or not all(isinstance(e, str) for e in data["backup_exclude"]):
        raise TemplateError("HRI's app/config.yaml: backup_exclude is not a list of strings")
    return data


def stamp(template: dict, name: str, version: str, channel: str) -> dict:
    names.validate_name(name)
    if channel not in children.CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    out: dict[str, Any] = {}
    prefix = f"*_{names.folder_name(name)}/"
    for key, value in template.items():
        if key == "webui" or (key == "image" and channel == "git"):
            continue
        if key == "name":
            value = names.display_name(name)
        elif key == "slug":
            value = names.config_slug(name)
        elif key == "version":
            value = version
        elif key == "panel_title":
            value = names.panel_title(name)
        elif key == "ports":
            value = {port: None for port in value}
        elif key == "backup_exclude":
            value = [prefix + e[len(HRI_BACKUP_PREFIX):] if e.startswith(HRI_BACKUP_PREFIX) else e for e in value]
        out[key] = value
    if any(names.HRI_SLUG in e for e in out["backup_exclude"]):
        raise TemplateError("a backup_exclude entry names HRI's slug elsewhere than at its start: not stamped")
    return out


def dump(config: dict, source: str) -> bytes:
    head = (f"# Written by HRI Manager from {source}.\n"
            "# Do not edit: the manager rewrites this folder on every update.\n")
    return (head + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)).encode("utf-8")


def _template(archive: tarsafe.Archive) -> dict:
    if "app/config.yaml" not in archive.files:
        raise TemplateError("the archive has no app/config.yaml: not hass-remote-integration, or older than its app")
    return parse_template(archive.read("app/config.yaml"))


def _app_extras(archive: tarsafe.Archive) -> dict[str, bytes]:
    """DOCS.md, CHANGELOG.md, icons and translations of app/, by their path relative to app/."""
    out = {}
    for rel in archive.files:
        if not rel.startswith("app/"):
            continue
        sub = rel[len("app/"):]
        if sub in APP_FILES or (sub.startswith("translations/") and sub.count("/") == 1
                                and os.path.splitext(sub)[1] in CONFIG_SUFFIXES):
            out[sub] = archive.read(rel)
    return out


def build_release(archive: tarsafe.Archive, dest: str, name: str, version: str, source: str) -> dict:
    """Fill ``dest`` with a release instance's definition: the stamped config and HRI's app files."""
    config = stamp(_template(archive), name, version, "release")
    children.write_file(dest, "config.yaml", dump(config, source))
    for sub, data in sorted(_app_extras(archive).items()):
        children.write_file(dest, sub, data)
    return config


def _is_config_name(filename: str) -> bool:
    stem, ext = os.path.splitext(filename)
    return stem in ("config", "repository") and ext in CONFIG_SUFFIXES


def build_git(archive: tarsafe.Archive, dest: str, name: str, version: str, sha: str, source: str) -> tuple[dict, list[str]]:
    """Fill ``dest`` with a git instance: HRI's whole tree, built by the Supervisor from its root Dockerfile.

    Every config.{yaml,yml,json} and repository.* of the tree goes (app/config.yaml included): the store reads each
    config.* as an app, and a second one would be a second app.  Only those suffixes: HRI has static/config.js,
    config.css and templates/config.html, which the build needs.  Returns the stamped config and what was done."""
    if "Dockerfile" not in archive.files:
        raise TemplateError("the archive has no Dockerfile at its root: the Supervisor could not build it")
    config = stamp(_template(archive), name, version, "git")
    extras = _app_extras(archive)
    tarsafe.extract(archive, dest)
    notes = [f"skipped link {rel}" for rel in archive.skipped]
    for folder, dirnames, filenames in os.walk(dest):
        for filename in filenames:
            if _is_config_name(filename):
                path = os.path.join(folder, filename)
                os.unlink(path)
                notes.append(f"removed {os.path.relpath(path, dest)}")
    for sub, data in sorted(extras.items()):
        path = children.safe_join(dest, sub)
        if os.path.lexists(path):
            os.unlink(path)
        children.write_file(dest, sub, data)
    children.write_file(dest, "config.yaml", dump(config, source))
    dockerfile = os.path.join(dest, "Dockerfile")
    with open(dockerfile, "rb") as fh:
        text = fh.read()
    patched, count = HRI_BUILD_ARG.subn(b"ARG HRI_BUILD=" + sha.encode("ascii"), text, count=1)
    if count:
        os.unlink(dockerfile)
        children.write_file(dest, "Dockerfile", patched)
        notes.append(f"Dockerfile: HRI_BUILD defaults to {sha[:12]}")
    else:
        notes.append("Dockerfile: no 'ARG HRI_BUILD=local' line; the build shows as 'local'")
    return config, notes


def find_configs(root: str) -> list[str]:
    """The config.* files the Supervisor's store would read below ``root`` (supervisor/store/data.py)."""
    out = []
    for folder, dirnames, filenames in os.walk(root):
        rel_folder = os.path.relpath(folder, root)
        parts = [] if rel_folder == "." else rel_folder.split(os.sep)
        if any(p.startswith(".") or p == "rootfs" for p in parts):
            continue
        for filename in filenames:
            stem, ext = os.path.splitext(filename)
            if stem == "config" and ext in CONFIG_SUFFIXES and not filename.startswith("."):
                out.append("/".join(parts + [filename]))
    return sorted(out)
