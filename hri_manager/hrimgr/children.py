"""The instance folders under the local apps folder, and the marker that makes one the manager's.

Every write stays inside ``<local apps>/hri_<name>/``: a folder is built in a hidden temporary folder next to it
(``.hri-tmp-*``: the Supervisor's store skips every path part that starts with a dot) and renamed into place, so the
store never reads half a definition.  No symlink is followed: a folder or marker that is a link is not the manager's.

The marker ``.hri-manager.json`` says that a folder, and the local app defined by it, belongs to the manager; the
manager's registry (registry.py, in its own /data, which the local apps folder cannot write) has to say so too, with
the same random ``instance_id``.  ``Managed`` is what ``load_managed`` returns after checking both, and the
Supervisor client refuses every call that changes an app unless it is given the ``Managed`` of that app's slug
(supervisor.py), checked again right before the call."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from typing import Callable

from . import names
from .registry import INSTANCE_ID_RE, Registry, RegistryError

MARKER = ".hri-manager.json"
MANAGER_ID = "hri_manager"
MAX_MARKER = 64 * 1024
TMP_PREFIX = ".hri-tmp-"  # a folder being built
OLD_PREFIX = ".hri-old-"  # the previous definition during an update, until it succeeds
DEL_PREFIX = ".hri-del-"  # a folder being deleted
CHANNELS = ("release", "git")


class NotManaged(Exception):
    """The folder or app is not one the manager created: it is left alone."""


class UnsafePath(Exception):
    pass


class DefinitionChanged(Exception):
    """A definition folder is no longer what the manager wrote (``check_tree``)."""


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _root(root: str) -> str:
    real = os.path.realpath(root)
    if not os.path.isdir(real):
        raise UnsafePath(f"{root} is not a folder: is the local apps folder mapped?")
    return real


def child_path(root: str, name: str) -> str:
    """``<root>/hri_<name>``, refused when it is (or would be) anything but a real folder directly in ``root``."""
    names.validate_name(name)
    real_root = _root(root)
    path = os.path.join(real_root, names.folder_name(name))
    if os.path.dirname(os.path.realpath(path)) != real_root and os.path.lexists(path):
        raise UnsafePath(f"{names.folder_name(name)} is a link out of the local apps folder")
    if os.path.islink(path):
        raise UnsafePath(f"{names.folder_name(name)} is a link, not a folder")
    return path


def validate_marker(data: object, name: str) -> dict:
    if not isinstance(data, dict):
        raise NotManaged("the marker is not a JSON object")
    if data.get("manager") != MANAGER_ID:
        raise NotManaged("the marker was not written by HRI Manager")
    if data.get("name") != name:
        raise NotManaged(f"the marker names {data.get('name')!r}, not {name!r}")
    if data.get("slug") != names.supervisor_slug(name):
        raise NotManaged(f"the marker's slug {data.get('slug')!r} is not {names.supervisor_slug(name)!r}")
    if data.get("channel") not in CHANNELS:
        raise NotManaged(f"the marker's channel {data.get('channel')!r} is unknown")
    if not isinstance(data.get("instance_id"), str) or not INSTANCE_ID_RE.fullmatch(data["instance_id"]):
        raise NotManaged("the marker has no instance id")
    if not isinstance(data.get("template_source"), str) or not names.SOURCE_RE.fullmatch(data["template_source"]):
        raise NotManaged("the marker's source is not a source URL")
    return data


def check_registry(marker: dict, registry: Registry, name: str) -> dict:
    """The registry's entry for ``name``, when it agrees with the marker; NotManaged otherwise."""
    if not isinstance(registry, Registry):
        raise NotManaged("no registry to check the marker against")
    try:
        entry = registry.get(name)
    except RegistryError as err:
        raise NotManaged(str(err)) from None
    if entry is None:
        raise NotManaged(f"{names.folder_name(name)} has a marker, but the manager's registry has no instance {name}: "
                         "not created by this manager")
    for key in ("instance_id", "slug", "channel"):
        if entry.get(key) != marker.get(key):
            raise NotManaged(f"the marker of {names.folder_name(name)} and the manager's registry disagree ({key})")
    return entry


def read_marker(folder: str, name: str) -> dict:
    path = os.path.join(folder, MARKER)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise NotManaged(f"{names.folder_name(name)} has no {MARKER}") from None
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise NotManaged(f"{MARKER} of {names.folder_name(name)} is a link") from None
        raise NotManaged(f"{MARKER} of {names.folder_name(name)} cannot be read: {err.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_MARKER:
            raise NotManaged(f"{MARKER} of {names.folder_name(name)} is not a small regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_MARKER + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NotManaged(f"{MARKER} of {names.folder_name(name)} is not JSON") from None
    return validate_marker(data, name)


@dataclass(frozen=True)
class Managed:
    """An instance folder whose marker was checked: the capability every changing Supervisor call needs."""

    root: str
    name: str
    marker: dict = field(compare=False, hash=False, repr=False)
    registry: Registry | None = field(default=None, compare=False, hash=False, repr=False)
    entry: dict = field(default_factory=dict, compare=False, hash=False, repr=False)
    # what the manager wrote (digest_tree), when this Managed comes from write_new: checked again before the store
    # reads the folder and before the install or update
    manifest: dict | None = field(default=None, compare=False, hash=False, repr=False)

    @property
    def slug(self) -> str:
        return names.supervisor_slug(self.name)

    def verify(self) -> None:
        """Check the marker on disk and the registry again, right before a call: either may have changed since."""
        load_managed(self.root, self.name, self.registry)


def load_managed(root: str, name: str, registry: Registry | None) -> Managed:
    names.validate_name(name)
    try:
        path = child_path(root, name)
    except UnsafePath as err:
        raise NotManaged(str(err)) from None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise NotManaged(f"no folder {names.folder_name(name)} in the local apps folder") from None
    if not stat.S_ISDIR(st.st_mode):
        raise NotManaged(f"{names.folder_name(name)} is not a folder")
    marker = read_marker(path, name)
    entry = check_registry(marker, registry, name)
    return Managed(root=root, name=name, marker=marker, registry=registry, entry=entry)


def scan(root: str, registry: Registry) -> list[tuple[str, dict | None, str | None]]:
    """Every ``hri_<name>`` entry of the local apps folder: (name, marker or None, why it is not managed)."""
    out = []
    try:
        real_root = _root(root)
        entries = sorted(os.listdir(real_root))
    except (OSError, UnsafePath):
        return out
    for entry in entries:
        if not entry.startswith(names.FOLDER_PREFIX):
            continue
        name = entry[len(names.FOLDER_PREFIX):]
        try:
            names.validate_name(name)
        except names.InvalidName:
            continue
        try:
            out.append((name, load_managed(root, name, registry).marker, None))
        except NotManaged as err:
            out.append((name, None, str(err)))
    return out


def safe_join(base: str, rel: str) -> str:
    """``base/rel`` for a relative POSIX path with no empty, ``.`` or ``..`` part."""
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or any(p in ("", ".", "..") for p in parts):
        raise UnsafePath(f"refused path {rel!r}")
    return os.path.join(base, *parts)


def make_dirs(base: str, rel: str) -> str:
    """Create ``base/rel`` folder by folder, refusing any part that exists as something other than a real folder."""
    path = base
    for part in rel.split("/") if rel else []:
        path = safe_join(path, part)
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                raise UnsafePath(f"{rel!r} crosses a non-folder") from None
    return path


def write_file(base: str, rel: str, data: bytes, mode: int = 0o644) -> None:
    """Write a new file below ``base``; never through a link, never over an existing file."""
    parent, _, leaf = rel.rpartition("/")
    folder = make_dirs(base, parent) if parent else base
    path = safe_join(folder, leaf)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def marker_bytes(marker: dict) -> bytes:
    return (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _tmp_dir(real_root: str, name: str) -> str:
    path = os.path.join(real_root, f"{TMP_PREFIX}{name}-{secrets.token_hex(4)}")
    os.mkdir(path, 0o755)
    return path


def _remove_tree(path: str) -> None:
    # rmtree removes links, it does not follow them
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.lexists(path):
        shutil.rmtree(path)


def write_new(root: str, name: str, build: Callable[[str], dict], registry: Registry) -> Managed:
    """Build a new instance folder with ``build(tmp)`` (which fills the folder and returns the marker) and rename
    it into place.  Refused when anything named ``hri_<name>`` exists.  The caller has put the registry's entry."""
    final = child_path(root, name)
    if os.path.lexists(final):
        raise UnsafePath(f"{names.folder_name(name)} already exists in the local apps folder")
    tmp = _tmp_dir(os.path.dirname(final), name)
    try:
        marker = validate_marker(build(tmp), name)
        write_file(tmp, MARKER, marker_bytes(marker))
        manifest = digest_tree(tmp)
        if os.path.lexists(final):
            raise UnsafePath(f"{names.folder_name(name)} appeared while it was being written")
        os.rename(tmp, final)
    except BaseException:
        _remove_tree(tmp)
        raise
    return dataclasses.replace(load_managed(root, name, registry), manifest=manifest)


class Replacement:
    """A managed folder swapped for a new build, with the previous one kept until ``commit`` (or put back by
    ``rollback``)."""

    def __init__(self, root: str, name: str, final: str, old: str, manifest: dict | None = None):
        self.root, self.name, self.final, self.old = root, name, final, old
        self.manifest = manifest  # the new build, as digest_tree saw it before it was renamed into place

    def commit(self) -> None:
        _remove_tree(self.old)

    def rollback(self) -> None:
        if not os.path.lexists(self.old):
            return
        _remove_tree(self.final)
        os.rename(self.old, self.final)


def replace(managed: Managed, build: Callable[[str], dict]) -> Replacement:
    managed.verify()
    final = child_path(managed.root, managed.name)
    real_root = os.path.dirname(final)
    tmp = _tmp_dir(real_root, managed.name)
    old = os.path.join(real_root, f"{OLD_PREFIX}{managed.name}-{secrets.token_hex(4)}")
    try:
        marker = validate_marker(build(tmp), managed.name)
        write_file(tmp, MARKER, marker_bytes(marker))
        manifest = digest_tree(tmp)
        os.rename(final, old)
        try:
            os.rename(tmp, final)
        except BaseException:
            os.rename(old, final)
            raise
    except BaseException:
        _remove_tree(tmp)
        raise
    return Replacement(managed.root, managed.name, final, old, manifest)


def remove(managed: Managed) -> None:
    managed.verify()
    final = child_path(managed.root, managed.name)
    # renamed out of the store's sight first: a half-removed folder is never read as an app
    doomed = os.path.join(os.path.dirname(final), f"{DEL_PREFIX}{managed.name}-{secrets.token_hex(4)}")
    os.rename(final, doomed)
    _remove_tree(doomed)


def digest_tree(folder: str) -> dict[str, str]:
    """Every entry below ``folder``, never through a link: its relative POSIX path -> ``sha256:<hex>`` for a file,
    ``link:<target>`` for a symlink, ``dir`` for a folder, ``other`` for anything else."""
    out: dict[str, str] = {}
    for current, dirnames, filenames in os.walk(folder):  # links to folders are listed, not followed
        rel_dir = os.path.relpath(current, folder)
        for entry in dirnames + filenames:
            path = os.path.join(current, entry)
            rel = entry if rel_dir == "." else f"{rel_dir.replace(os.sep, '/')}/{entry}"
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                out[rel] = "link:" + os.readlink(path)
            elif stat.S_ISDIR(mode):
                out[rel] = "dir"
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
                out[rel] = "sha256:" + digest.hexdigest()
            else:
                out[rel] = "other"
    return out


def check_tree(root: str, name: str, manifest: dict[str, str]) -> None:
    """DefinitionChanged unless ``hri_<name>/`` holds exactly what ``manifest`` (digest_tree of what the manager
    wrote) says: anyone who can write the local apps folder can change a definition between its write and the
    Supervisor's reading of it."""
    try:
        found = digest_tree(child_path(root, name))
    except (OSError, UnsafePath) as err:
        raise DefinitionChanged(f"{names.folder_name(name)} cannot be read again: {err}") from None
    if found != manifest:
        changed = sorted(k for k in set(found) | set(manifest) if found.get(k) != manifest.get(k))
        shown = ", ".join(repr(c[:80]) for c in changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        raise DefinitionChanged(f"{names.folder_name(name)} was changed after the manager wrote it ({shown})")


def newest_future(root: str, slack: float = 5.0) -> tuple[str, float] | None:
    """The newest file or folder of the local apps folder when it is dated in the future: (relative path, mtime).  The
    Supervisor notices a change there only when the newest date changes (utils get_latest_mtime), so such a file hides
    every later change to any local app."""
    try:
        real_root = _root(root)
    except UnsafePath:
        return None
    newest: tuple[str, float] | None = None
    for folder, dirnames, filenames in os.walk(real_root):
        for entry in dirnames + filenames:
            path = os.path.join(folder, entry)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest[1]:
                newest = (os.path.relpath(path, real_root), mtime)
    return newest if newest and newest[1] > _dt.datetime.now().timestamp() + slack else None


_OLD_RE = re.compile(re.escape(OLD_PREFIX) + r"(" + names.NAME_RE.pattern + r")-[0-9a-f]{8}")


def cleanup_stale(root: str) -> list[str]:
    """Tidy what a crash left behind, touching only the manager's own hidden names: a folder being built or deleted
    goes; a previous definition goes too, unless the update stopped between its two renames and it is the only
    definition left, which is then put back."""
    done = []
    try:
        real_root = _root(root)
        entries = sorted(os.listdir(real_root))
    except (OSError, UnsafePath):
        return done
    for entry in entries:
        if not entry.startswith((TMP_PREFIX, OLD_PREFIX, DEL_PREFIX)):
            continue
        path = os.path.join(real_root, entry)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        m = _OLD_RE.fullmatch(entry)
        final = os.path.join(real_root, names.folder_name(m.group(1))) if m else None
        if final and not os.path.lexists(final):
            os.rename(path, final)
            done.append(f"restored {entry}")
        else:
            shutil.rmtree(path, ignore_errors=True)
            done.append(f"removed {entry}")
    return done
