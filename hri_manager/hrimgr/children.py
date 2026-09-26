"""The instance folders under the local apps folder, and the marker that makes one the manager's.

Every write stays inside ``<local apps>/hri_<name>/``: a folder is built in a hidden temporary folder next to it
(``.hri-tmp-*``: the Supervisor's store skips every path part that starts with a dot) and renamed into place, so the
store never reads half a definition.  No symlink is followed: a folder or marker that is a link is not the manager's,
and every file below a folder is created, read or removed through folder descriptors opened part by part with
O_NOFOLLOW (``_folder_fd``), so a part swapped for a link while the manager writes is refused, never followed.

The marker ``.hri-manager.json`` says that a folder, and the local app defined by it, belongs to the manager; the
manager's registry (registry.py, in its own /data, which the local apps folder cannot write) has to say so too, with
the same random ``instance_id``.  ``Managed`` is what ``load_managed`` returns after checking both, and the
Supervisor client refuses every call that changes an app unless it is given the ``Managed`` of that app's slug
(supervisor.py), checked again right before the call."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import errno
import fcntl
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
MAX_DEPTH = 64  # folders below a definition's folder (walk): HRI's tree is far shallower


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


def open_regular(path: str, dir_fd: int | None = None, follow: bool = False) -> int:
    """A descriptor of the regular file ``path`` (relative to ``dir_fd``), for reading: opened without following a
    link (unless ``follow``: what a link points at, as the Supervisor reads a file) and without blocking (a FIFO put
    in its place would block an open forever), then checked with fstat.  UnsafePath for anything but a regular file;
    OSError as os.open raises it."""
    fd = os.open(path, os.O_RDONLY | (0 if follow else _NOFOLLOW) | _CLOEXEC | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePath(f"{os.path.basename(path)!r} is not a regular file")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_marker(folder: str, name: str) -> dict:
    try:
        folder_fd = os.open(folder, _DIR_FLAGS)
    except FileNotFoundError:
        raise NotManaged(f"no folder {names.folder_name(name)} in the local apps folder") from None
    except OSError as err:
        if err.errno in (errno.ELOOP, errno.ENOTDIR):
            raise NotManaged(f"{names.folder_name(name)} is not a folder") from None
        raise NotManaged(f"{names.folder_name(name)} cannot be read: {err.strerror}") from None
    try:
        fd = open_regular(MARKER, folder_fd)
    except FileNotFoundError:
        raise NotManaged(f"{names.folder_name(name)} has no {MARKER}") from None
    except UnsafePath:
        raise NotManaged(f"{MARKER} of {names.folder_name(name)} is not a small regular file") from None
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise NotManaged(f"{MARKER} of {names.folder_name(name)} is a link") from None
        raise NotManaged(f"{MARKER} of {names.folder_name(name)} cannot be read: {err.strerror}") from None
    finally:
        os.close(folder_fd)
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


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC


def _parts(rel: str) -> list[str]:
    """The parts of a relative POSIX path with no empty, ``.`` or ``..`` part."""
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or any(p in ("", ".", "..") for p in parts):
        raise UnsafePath(f"refused path {rel!r}")
    return parts


def safe_join(base: str, rel: str) -> str:
    """``base/rel`` for a relative POSIX path with no empty, ``.`` or ``..`` part."""
    return os.path.join(base, *_parts(rel))


def _open_folder(path: str, dir_fd: int | None, rel: str) -> int:
    try:
        return os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as err:
        if err.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(f"{rel!r} crosses a non-folder") from None
        raise


def _folder_fd(base: str, rel: str, create: bool) -> int:
    """An open descriptor of the folder ``base/rel``, opened part by part below ``base`` (each part created 0755 when
    ``create``), every part with O_NOFOLLOW: a part that is, or is swapped for, anything but a real folder is refused
    (UnsafePath), never followed.  The caller closes it."""
    fd = _open_folder(base, None, rel)
    try:
        for part in _parts(rel) if rel else []:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            inner = _open_folder(part, fd, rel)
            os.close(fd)
            fd = inner
    except BaseException:
        os.close(fd)
        raise
    return fd


def _in_folder(base: str, rel: str, create: bool) -> tuple[int, str]:
    """(descriptor of the folder holding ``rel`` below ``base``, the leaf's name)."""
    parent, _, leaf = rel.rpartition("/")
    _parts(rel)
    return _folder_fd(base, parent, create), leaf


class BuildFolder(str):
    """The path of a folder being built (write_new, replace): every entry this module writes into it, the folders it
    creates on the way, and every entry it removes are recorded in ``written`` (relative path -> what digest_tree calls
    it, without inode and time), so that what the folder holds when the build returns can be compared with what the
    manager wrote (_check_written), not taken as it is: another writer of the local apps folder may add or change
    files during a build that takes seconds."""

    written: dict[str, str]

    def __new__(cls, path: str) -> "BuildFolder":
        obj = super().__new__(cls, path)
        obj.written = {}
        return obj


def _record(base: str, rel: str, kind: str | None) -> None:
    """What was written at ``rel`` below ``base`` (and the folders above it), when ``base`` is a BuildFolder; None:
    removed."""
    written = getattr(base, "written", None)
    if written is None:
        return
    parts = _parts(rel)
    for i in range(1, len(parts)):
        written["/".join(parts[:i])] = "dir"
    if kind is None:
        written.pop(rel, None)
    else:
        written[rel] = kind


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def make_dirs(base: str, rel: str) -> str:
    """Create ``base/rel`` folder by folder, refusing any part that exists as something other than a real folder."""
    os.close(_folder_fd(base, rel, True))
    if rel:
        _record(base, rel, "dir")
    return safe_join(base, rel) if rel else base


def write_file(base: str, rel: str, data: bytes, mode: int = 0o644) -> None:
    """Write a new file below ``base``; never through a link, never over an existing file."""
    folder_fd, leaf = _in_folder(base, rel, True)
    try:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, mode, dir_fd=folder_fd)
    finally:
        os.close(folder_fd)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    _record(base, rel, _sha(data))


def replace_file(base: str, rel: str, data: bytes, mode: int = 0o644) -> None:
    """Put ``data`` in place of the file ``rel`` below ``base`` at once: written to a hidden new file next to it (the
    store skips names starting with a dot), then renamed over it, both through the holding folder's descriptor."""
    folder_fd, leaf = _in_folder(base, rel, False)
    tmp = f".hri-new-{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, mode, dir_fd=folder_fd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, leaf, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=folder_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(folder_fd)
    _record(base, rel, _sha(data))


def make_link(base: str, rel: str, target: str) -> None:
    """A new symlink ``rel`` below ``base`` pointing at ``target`` (checked by the caller)."""
    folder_fd, leaf = _in_folder(base, rel, True)
    try:
        os.symlink(target, leaf, dir_fd=folder_fd)
    finally:
        os.close(folder_fd)
    _record(base, rel, "link:" + target)


def remove_file(base: str, rel: str) -> None:
    """Remove the file or link ``rel`` below ``base`` (a link itself, not what it points at)."""
    folder_fd, leaf = _in_folder(base, rel, False)
    try:
        os.unlink(leaf, dir_fd=folder_fd)
    finally:
        os.close(folder_fd)
    _record(base, rel, None)


def list_folder(base: str, rel: str) -> list[str]:
    """The names in the folder ``rel`` below ``base``, reached without following a link."""
    fd = _folder_fd(base, rel, False)
    try:
        return os.listdir(fd)
    finally:
        os.close(fd)


def read_file(base: str, rel: str) -> bytes:
    """The regular file ``rel`` below ``base``, never through a link."""
    folder_fd, leaf = _in_folder(base, rel, False)
    try:
        fd = open_regular(leaf, folder_fd)
    finally:
        os.close(folder_fd)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def marker_bytes(marker: dict) -> bytes:
    return (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _tmp_dir(real_root: str, name: str) -> BuildFolder:
    # 0700 while it is built (nobody but its owner lists it); 0755, as any app folder, once checked (_finish_build)
    path = os.path.join(real_root, f"{TMP_PREFIX}{name}-{secrets.token_hex(4)}")
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)  # whatever the umask
    return BuildFolder(path)


def _finish_build(tmp: BuildFolder, name: str, marker: dict) -> dict[str, str]:
    """The marker written, the folder's manifest (digest_tree), checked against what the manager wrote into it
    (BuildFolder.written): the same names, and for each the same bytes, link or folder.  DefinitionChanged
    otherwise: something else wrote in the folder during the build."""
    write_file(tmp, MARKER, marker_bytes(marker))
    manifest = digest_tree(tmp)
    found = {rel: value.rsplit(" ", 2)[0] for rel, value in manifest.items() if rel != "."}
    if found != tmp.written:
        changed = sorted(k for k in set(found) | set(tmp.written) if found.get(k) != tmp.written.get(k))
        shown = ", ".join(repr(c[:80]) for c in changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        raise DefinitionChanged(f"{names.folder_name(name)} was being written, and it holds what the manager did not "
                                f"write ({shown}): refused. Anyone who can write the local apps folder can add or change "
                                "files there; find out who did")
    os.chmod(tmp, 0o755)
    return manifest


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
        manifest = _finish_build(tmp, name, validate_marker(build(tmp), name))
        if os.path.lexists(final):
            raise UnsafePath(f"{names.folder_name(name)} appeared while it was being written")
        os.rename(tmp, final)
        manifest["."] = folder_identity(final)
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
        # renamed out first: a commit stopped midway leaves a folder being deleted, never half a previous definition
        # that cleanup_stale would put back
        doomed = os.path.join(os.path.dirname(self.old), f"{DEL_PREFIX}{self.name}-{secrets.token_hex(4)}")
        os.rename(self.old, doomed)
        _remove_tree(doomed)

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
        manifest = _finish_build(tmp, managed.name, validate_marker(build(tmp), managed.name))
        os.rename(final, old)
        try:
            os.rename(tmp, final)
        except BaseException:
            os.rename(old, final)
            raise
        manifest["."] = folder_identity(final)
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


def _identity(st: os.stat_result) -> str:
    return f"ino:{st.st_ino} ctime:{st.st_ctime_ns}"


def folder_identity(folder: str) -> str:
    """The manifest entry of a folder itself (``.``): taken again after the rename that puts it in place, which
    changes its change time."""
    return "dir " + _identity(os.lstat(folder))


def walk(base: str, skip: Callable[[str], bool] | None = None, max_depth: int = MAX_DEPTH,
         max_entries: int | None = None):
    """Every entry below the folder ``base``, depth first in name order, as (relative POSIX path, its name, a
    descriptor of the folder holding it, its stat): each folder is opened from its parent's descriptor with O_NOFOLLOW,
    so a folder swapped for a link after it was listed is refused (UnsafePath), never followed, and a link is listed,
    never followed.  A folder's stat is its descriptor's, taken after it was opened (what is walked is what is
    described).  ``skip(name)``: an entry neither listed nor walked into.  Deeper than ``max_depth``, or more than
    ``max_entries`` entries: UnsafePath.  The descriptors are valid only until the next entry is asked for."""
    count = [0]

    def folder(fd: int, rel_dir: str, depth: int):
        for name in sorted(os.listdir(fd)):
            if skip is not None and skip(name):
                continue
            count[0] += 1
            if max_entries is not None and count[0] > max_entries:
                raise UnsafePath(f"more than {max_entries} entries")
            rel = f"{rel_dir}/{name}" if rel_dir else name
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                yield rel, name, fd, st
                continue
            if depth >= max_depth:
                raise UnsafePath(f"{rel[:80]!r} is more than {max_depth} folders deep")
            sub = _open_folder(name, fd, rel)
            try:
                yield rel, name, fd, os.fstat(sub)
                yield from folder(sub, rel, depth + 1)
            finally:
                os.close(sub)

    top = _open_folder(base, None, ".")
    try:
        yield from folder(top, "", 0)
    finally:
        os.close(top)


def digest_tree(folder: str) -> dict[str, str]:
    """Every entry below ``folder``, and ``.`` for the folder itself, never through a link (``walk``): its relative
    POSIX path -> what it is (``sha256:<hex>`` for a file, ``link:<target>`` for a symlink, ``dir`` for a folder,
    ``other`` for anything else), its inode and its change time.  The change time moves on every write, rename, link or
    metadata change of an entry (a folder's, on every entry added to or removed from it) and user space cannot set it
    back: a definition changed and put back as it was, around the Supervisor's reading of it, differs here."""
    out: dict[str, str] = {".": folder_identity(folder)}
    for rel, name, parent, st in walk(folder):
        if stat.S_ISLNK(st.st_mode):
            kind = "link:" + os.readlink(name, dir_fd=parent)
        elif stat.S_ISDIR(st.st_mode):
            kind = "dir"
        elif stat.S_ISREG(st.st_mode):
            digest = hashlib.sha256()
            with os.fdopen(open_regular(name, parent), "rb") as fh:
                st = os.fstat(fh.fileno())  # the identity of what is hashed
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            kind = "sha256:" + digest.hexdigest()
        else:
            kind = "other"
        out[rel] = f"{kind} {_identity(st)}"
    return out


def check_tree(root: str, name: str, manifest: dict[str, str]) -> None:
    """DefinitionChanged unless ``hri_<name>/`` holds exactly what ``manifest`` (digest_tree of what the manager
    wrote) says: anyone who can write the local apps folder can change a definition between its write and the
    Supervisor's reading of it."""
    try:
        found = digest_tree(child_path(root, name))
    except (OSError, UnsafePath) as err:  # a folder swapped for a link, or a tree deeper than MAX_DEPTH, among them
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


def pending_mark(reason: str) -> dict:
    """The registry's mark of an installed app the manager has not checked yet (it was installed while the manager was
    not watching): it keeps running, and the manager checks it (at its start, or Check again on its row)."""
    return {"reason": reason[:500], "at": now_iso(), "unverified": True, "pending": True, "uninstalled": False,
            "stopped": False, "failure": None}


def _update_state(real_root: str, name: str, registry: Registry | None, installed: dict[str, str | None] | None) -> str:
    """What to do with an update of ``name`` that stopped between its swap and its record (the registry's ``updating``
    flag, set before the swap and cleared when the update is recorded or undone): "keep" the new definition when the
    Supervisor has installed its version (putting the previous one back would offer a downgrade, and install it with
    auto-update on), "restore" the previous one when the Supervisor still has another version, "leave" both when it
    could not be asked (``installed`` None), "none" when there is no such update."""
    try:
        entry = registry.get(name) if isinstance(registry, Registry) else None
    except RegistryError:
        return "none"
    if not entry or not isinstance(entry.get("updating"), dict):
        return "none"
    try:
        marker = read_marker(os.path.join(real_root, names.folder_name(name)), name)
    except NotManaged:
        return "restore"  # not the manager's definition in place: the previous one is
    if marker.get("version") == entry.get("version"):
        return "none"
    if installed is None:
        return "leave"
    fields = entry["updating"].get("fields") if isinstance(entry["updating"].get("fields"), dict) else {}
    if installed.get(name) is not None and installed.get(name) == marker.get("version") == fields.get("version"):
        return "keep"
    return "restore"


def settle_update(registry: Registry, name: str, entry: dict, marker: dict, installed_version: str | None) -> str | None:
    """An ``updating`` flag left after the swap (no previous definition to put back any more).  When the definition in
    place (``marker``) is the one the flag names (its version, commit and stamping, from the registry itself) and the
    Supervisor has installed that version, the registry records it now, marked to be checked (pending_mark);
    when the Supervisor has another version, or the definition in place is the registry's own, the flag goes; when
    the installed version is not known (None), nothing changes.  What was done."""
    target = entry.get("updating")
    if not isinstance(target, dict):
        return None
    fields = target.get("fields") if isinstance(target.get("fields"), dict) else {}

    def key(d: dict) -> tuple:
        return d.get("version"), d.get("sha"), d.get("stamp_version")

    if fields and key(marker) == key(fields):
        if installed_version is None:
            return None
        if installed_version == fields.get("version"):
            registry.update(name, updating=None, **{**fields, "tampered": pending_mark(
                f"its update to {fields.get('version')} was recorded late: the manager had stopped before it compared "
                "the installed app with the definition it wrote")})
            return f"{name}: its update to {fields.get('version')} had finished; the registry records it now"
        registry.update(name, updating=None)
        return f"{name}: its update to {fields.get('version')} was not installed; its flag is cleared"
    if (marker.get("version"), marker.get("stamp_version")) == (entry.get("version"), entry.get("stamp_version")):
        registry.update(name, updating=None)
        return f"{name}: its unfinished update had changed nothing; its flag is cleared"
    return None


def _settle_updates(real_root: str, registry: Registry, done: list[str], installed: dict[str, str | None]) -> None:
    """settle_update for every flag without a previous definition left (see cleanup_stale)."""
    for name, entry in registry.all().items():
        if not isinstance(entry.get("updating"), dict) or any(
                e.startswith(f"{OLD_PREFIX}{name}-") for e in os.listdir(real_root)):
            continue
        try:
            marker = read_marker(os.path.join(real_root, names.folder_name(name)), name)
        except NotManaged:
            continue
        note = settle_update(registry, name, entry, marker, installed.get(name))
        if note:
            done.append(note)


def cleanup_stale(root: str, registry: Registry | None = None, installed: dict[str, str | None] | None = None) -> list[str]:
    """Tidy what a crash left behind, touching only the manager's own hidden names: a folder being built or deleted
    goes; a previous definition goes too, unless it is the only definition left (put back), or the update that set
    it aside stopped before it was recorded (_update_state): then the new definition stays when the Supervisor has
    installed it (recorded, and marked to be checked), the previous one is put back when the Supervisor has another
    version, and both stay when ``installed`` (name -> installed version, from the Supervisor) is None: it could not
    be asked.  Never raises for one entry: what cannot be tidied is reported and the manager starts anyway."""
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
        name = m.group(1) if m else None
        final = os.path.join(real_root, names.folder_name(name)) if name else None
        try:
            state = _update_state(real_root, name, registry, installed) if final and os.path.lexists(final) else "none"
            if final and not os.path.lexists(final):
                os.rename(path, final)
                done.append(f"restored {entry}")
            elif state == "restore":
                _remove_tree(final)
                os.rename(path, final)
                registry.update(name, updating=None)
                done.append(f"restored {entry}: the update of {name} stopped before it was recorded, and the "
                            "Supervisor has not installed it")
            elif state == "keep":
                note = settle_update(registry, name, registry.get(name), read_marker(final, name), installed.get(name))
                shutil.rmtree(path, ignore_errors=True)
                done.append(note or f"removed {entry}")
            elif state == "leave":
                done.append(f"left {entry}: whether the Supervisor installed the update of {name} is not known (it "
                            "could not be asked); the next start settles it")
            else:
                shutil.rmtree(path, ignore_errors=True)
                done.append(f"removed {entry}")
        except (OSError, RegistryError, NotManaged) as err:
            done.append(f"could not tidy {entry}: {err}")
    if isinstance(registry, Registry) and installed is not None:
        try:
            _settle_updates(real_root, registry, done, installed)
        except (OSError, RegistryError) as err:
            done.append(f"could not settle the registry's unfinished updates: {err}")
    return done
    for entry in entries:
        if not entry.startswith((TMP_PREFIX, OLD_PREFIX, DEL_PREFIX)):
            continue
        path = os.path.join(real_root, entry)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        m = _OLD_RE.fullmatch(entry)
        name = m.group(1) if m else None
        final = os.path.join(real_root, names.folder_name(name)) if name else None
        try:
            if final and not os.path.lexists(final):
                os.rename(path, final)
                done.append(f"restored {entry}")
            elif final and _update_unfinished(real_root, name, registry):
                _remove_tree(final)
                os.rename(path, final)
                registry.update(name, updating=None)
                done.append(f"restored {entry}: the update of {name} stopped before it was committed")
            else:
                shutil.rmtree(path, ignore_errors=True)
                done.append(f"removed {entry}")
        except (OSError, RegistryError) as err:
            done.append(f"could not tidy {entry}: {err}")
    if isinstance(registry, Registry):
        try:
            _settle_updates(real_root, registry, done)
        except (OSError, RegistryError) as err:
            done.append(f"could not settle the registry's unfinished updates: {err}")
    return done
