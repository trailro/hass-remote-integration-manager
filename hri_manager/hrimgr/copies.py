"""A copy of each instance's definition in the manager's own ``/data`` (``definitions/<name>/``), which Repair writes
the definition from again when the local apps folder was lost: on current Supervisors a full backup leaves that folder
out, and ``/data`` is in the manager's backup.

- a release instance: its whole definition as the manager wrote it (the stamped config.yaml, HRI's DOCS.md,
  CHANGELOG.md, translations, and README.md and icons when HRI ships them): a few kilobytes, and Repair needs nothing
  from GitHub;
- a git instance: its stamped config.yaml only, never the source tree (megabytes); Repair downloads the source of the
  recorded commit again.

``copy.json`` says which instance (``instance_id``), channel, version, ref, commit and stamping version the copy is
of.  Repair uses a copy only when it agrees with the registry (instance id and channel) and with the installed app
(version), and when its config is an instance's definition this manager would write (``check``); otherwise it goes
to GitHub as before.  Nobody but the manager writes its ``/data`` (registry.py)."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass, field

import yaml

from . import children, names, stamp
from .registry import INSTANCE_ID_RE

DIR_NAME = "definitions"
META = "copy.json"
MAX_FILE = 1024 * 1024
MAX_TEXT = 256 * 1024  # DOCS.md, CHANGELOG.md, README.md, a translation
MAX_TOTAL = 4 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TMP_PREFIX = ".tmp-"
OLD_PREFIX = ".old-"
META_FIELDS = ("name", "instance_id", "channel", "version", "ref_kind", "ref", "sha", "stamp_version", "template_source")
# never translations/config.*: the store would read it as a second app
_TRANSLATION_RE = re.compile(r"translations/(?!config\.)[A-Za-z0-9_-]{1,40}\.(?:yaml|yml|json)")


class CopyError(Exception):
    """The copy is missing, of another instance, or not what the manager writes: Repair goes to GitHub."""


def is_copied(rel: str, channel: str) -> bool:
    """The files a copy holds, by their path in the instance's folder."""
    if rel == "config.yaml":
        return True
    return channel == "release" and (rel in stamp.APP_FILES or _TRANSLATION_RE.fullmatch(rel) is not None)


@dataclass
class Copy:
    meta: dict
    config: dict
    files: dict[str, bytes] = field(repr=False)

    @property
    def version(self) -> str:
        return self.meta["version"]

    @property
    def sha(self) -> str | None:
        return self.meta.get("sha")


def folder(root: str, name: str) -> str:
    names.validate_name(name)
    return os.path.join(root, name)


def _read(path: str, cap: int = MAX_FILE) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            raise CopyError(f"{os.path.basename(path)} is not a small regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(cap + 1)
    finally:
        os.close(fd)


def _walk(base: str) -> list[str]:
    out = []
    for current, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(current, base)
        for entry in filenames:
            out.append(entry if rel_dir == "." else f"{rel_dir.replace(os.sep, '/')}/{entry}")
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(current, d))]
    return sorted(out)


def save(root: str, name: str, instance_folder: str, marker: dict) -> list[str]:
    """Copy what ``is_copied`` names from ``instance_folder`` (just written by the manager) to ``root/<name>``,
    replacing any earlier copy.  Returns the files copied."""
    channel = marker.get("channel")
    files = {}
    for rel in _walk(instance_folder):
        if is_copied(rel, channel):
            files[rel] = _read(os.path.join(instance_folder, *rel.split("/")))
    if "config.yaml" not in files:
        raise CopyError(f"{names.folder_name(name)} has no config.yaml")
    if sum(len(d) for d in files.values()) > MAX_TOTAL:
        raise CopyError("the definition is larger than a copy may be")
    meta = {k: marker.get(k) for k in META_FIELDS}
    os.makedirs(root, mode=0o700, exist_ok=True)
    final = folder(root, name)
    tmp = os.path.join(root, f"{TMP_PREFIX}{name}-{secrets.token_hex(4)}")
    os.mkdir(tmp, 0o700)
    try:
        for rel, data in files.items():
            children.write_file(tmp, rel, data)
        children.write_file(tmp, META, (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        old = None
        if os.path.lexists(final):
            old = os.path.join(root, f"{OLD_PREFIX}{name}-{secrets.token_hex(4)}")
            os.rename(final, old)
        os.rename(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)
    return sorted(files)


def names_kept(root: str) -> list[str]:
    """The instance names the manager keeps a copy of."""
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    return sorted(e for e in entries if not e.startswith(".") and names.NAME_RE.fullmatch(e)
                  and e not in names.RESERVED and os.path.isdir(os.path.join(root, e)))


def remove(root: str, name: str) -> None:
    path = folder(root, name)
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def check_file(rel: str, data: bytes) -> None:
    """A copied file other than config.yaml: UTF-8 text within MAX_TEXT (a translation a YAML or JSON mapping), or a
    PNG icon."""
    if rel.endswith(".png"):
        if not data.startswith(PNG_MAGIC):
            raise CopyError(f"its {rel} is not a PNG")
        return
    if len(data) > MAX_TEXT:
        raise CopyError(f"its {rel} is larger than a copy's text may be")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CopyError(f"its {rel} is not UTF-8 text") from None
    if rel.startswith("translations/"):
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            raise CopyError(f"its {rel} does not parse") from None
        if not isinstance(parsed, dict):
            raise CopyError(f"its {rel} is not a mapping")


def check(config: object, name: str, version: str, channel: str) -> dict:
    """An instance's stamped definition, exactly as this manager writes it: its slug and version, HRI's url, the image
    only for a release, every other key vetted as HRI's own template is (stamp.vet_template), and stamping it again
    changes nothing (no name, panel title, port, backup_exclude entry or key stamping would not have written)."""
    if not isinstance(config, dict):
        raise CopyError("its config.yaml is not a mapping")
    if config.get("slug") != names.config_slug(name) or str(config.get("version")) != version:
        raise CopyError("its config.yaml is not the one of this instance and version")
    if config.get("url") != names.HRI_URL:
        raise CopyError("its config.yaml is not hass-remote-integration's")
    if (channel == "release") != ("image" in config):
        raise CopyError("its config.yaml does not match its channel (image)")
    try:
        stamp.vet_template({**config, "slug": names.HRI_SLUG})
        restamped = stamp.stamp({**config, "slug": names.HRI_SLUG}, name, version, channel)
    except (stamp.TemplateError, ValueError, TypeError, AttributeError) as err:
        raise CopyError(str(err)) from None
    if restamped != config:
        raise CopyError("its config.yaml is not what stamping writes")
    return config


def load(root: str, name: str, entry: dict, installed_version: str) -> Copy:
    """The copy of ``name``, when it is the copy of the registry's instance at the installed version."""
    base = folder(root, name)
    if not os.path.isdir(base) or os.path.islink(base):
        raise CopyError("the manager has no copy of it")
    try:
        meta = json.loads(_read(os.path.join(base, META)).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        raise CopyError(f"its {META} cannot be read: {err}") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("instance_id"), str) or not INSTANCE_ID_RE.fullmatch(meta["instance_id"]):
        raise CopyError(f"its {META} has an unexpected shape")
    source = meta.get("template_source")
    if not isinstance(source, str) or not names.SOURCE_RE.fullmatch(source):
        raise CopyError(f"its {META} names no source URL")
    if meta.get("instance_id") != entry.get("instance_id") or meta.get("name") != name:
        raise CopyError("it is the copy of another instance of that name")
    channel = meta.get("channel")
    if channel != entry.get("channel") or channel not in children.CHANNELS:
        raise CopyError(f"it is of the {channel} channel, the registry says {entry.get('channel')}")
    if meta.get("version") != installed_version:
        raise CopyError(f"it is of {meta.get('version')}, the installed app is {installed_version}")
    sha = meta.get("sha")
    if sha is not None and not (isinstance(sha, str) and names.SHA_RE.fullmatch(sha)):
        raise CopyError(f"its {META} names no commit")
    if channel == "git" and sha is None:
        raise CopyError(f"its {META} names no commit")
    files: dict[str, bytes] = {}
    try:
        for rel in _walk(base):
            if rel == META:
                continue
            if not is_copied(rel, channel):
                raise CopyError(f"it holds {rel!r}, which a copy does not")
            files[rel] = _read(os.path.join(base, *rel.split("/")))
            if sum(len(d) for d in files.values()) > MAX_TOTAL:
                raise CopyError("it is larger than a copy may be")
            if rel != "config.yaml":
                check_file(rel, files[rel])
        config = yaml.safe_load(files["config.yaml"].decode("utf-8")) if "config.yaml" in files else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        raise CopyError(f"it cannot be read: {err}") from None
    check(config, name, installed_version, channel)
    return Copy(meta=meta, config=config, files=files)
