"""Reading a GitHub source tarball without trusting it.

A tarball from codeload.github.com has one top folder (``<repo>-<ref>/``) and a pax global header whose comment is
the commit's SHA.  Every member is checked before anything is written: a relative POSIX name under the top folder,
no ``..``, no absolute path, only folders, regular files and symlinks; hard links, devices and FIFOs refuse the
whole archive; count and sizes are capped.  The gzip layer is unpacked first, as a stream capped at ``MAX_TOTAL`` plus
``HEADER_SLACK`` for the tar headers (a pax or GNU long-name header can claim gigabytes that ``tarfile`` would hold
in memory), and the caller runs all of it in a thread.  Member names in messages are cut and quoted (``show``).
Files are written by this module (never ``TarFile.extract``), so their
modification time is the time of writing: the Supervisor notices a changed local app by the newest mtime in the
folder, and a file dated in the future would hide every later change.  A symlink is recreated only when it points
at a regular file of the same archive; any other link is skipped and reported."""

from __future__ import annotations

import posixpath
import tarfile
import tempfile
import zlib
from dataclasses import dataclass, field

from . import children, names

MAX_COMPRESSED = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_MEMBERS = 20000
HEADER_SLACK = 32 * 1024 * 1024  # tar headers and padding: about 1 KiB per member at most, for MAX_MEMBERS
SPOOL = 16 * 1024 * 1024  # an unpacked archive larger than this goes to a temporary file instead of memory


class UnsafeArchive(Exception):
    pass


def show(name: str, limit: int = 120) -> str:
    """An archive member's name for a message or a log line: cut, and quoted with its escapes (repr)."""
    return repr(name if len(name) <= limit else name[:limit] + "…")


@dataclass
class Archive:
    sha: str | None
    top: str
    files: dict[str, tarfile.TarInfo] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)  # relative name -> relative target (a regular file)
    skipped: list[str] = field(default_factory=list)
    tar: tarfile.TarFile | None = None
    fileobj: object = None  # the unpacked tar (a SpooledTemporaryFile), which TarFile.close leaves open

    def close(self) -> None:
        if self.tar is not None:
            self.tar.close()
        if self.fileobj is not None:
            self.fileobj.close()

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def read(self, rel: str) -> bytes:
        info = self.files[rel]
        fh = self.tar.extractfile(info)
        if fh is None:
            raise UnsafeArchive(f"{show(rel)} cannot be read")
        data = fh.read(MAX_FILE + 1)
        if len(data) != info.size:
            raise UnsafeArchive(f"{show(rel)} is not the size its header says")
        return data


def _clean(name: str) -> str:
    if not name or "\0" in name or "\\" in name or name.startswith("/"):
        raise UnsafeArchive(f"refused member name {show(name)}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or not parts:
        raise UnsafeArchive(f"refused member name {show(name)}")
    return "/".join(parts)


def gunzip(data: bytes):
    """The tar inside a .tar.gz, unpacked as a stream and refused past MAX_TOTAL + HEADER_SLACK: a file object."""
    limit = MAX_TOTAL + HEADER_SLACK
    out = tempfile.SpooledTemporaryFile(max_size=SPOOL)
    size = 0
    inflater = zlib.decompressobj(wbits=31)  # gzip only
    tail = data
    try:
        while True:
            piece = inflater.decompress(tail, limit + 1 - size)
            size += len(piece)
            if size > limit:
                raise UnsafeArchive("the archive unpacks to more than the manager accepts")
            out.write(piece)
            tail = inflater.unconsumed_tail
            if inflater.eof or (not tail and not piece):
                break
    except zlib.error as err:
        out.close()
        raise UnsafeArchive(f"not a readable .tar.gz: {err}") from None
    except BaseException:
        out.close()
        raise
    if not inflater.eof:
        out.close()
        raise UnsafeArchive("not a readable .tar.gz: it ends too early")
    if inflater.unused_data.strip(b"\0"):
        out.close()
        raise UnsafeArchive("the archive has more data after its end")
    out.seek(0)
    return out


def open_archive(data: bytes) -> Archive:
    """Check and open a .tar.gz; blocking (the caller runs it in a thread)."""
    if len(data) > MAX_COMPRESSED:
        raise UnsafeArchive("the archive is larger than the manager accepts")
    fileobj = gunzip(data)
    try:
        archive = _open(fileobj)
    except BaseException:
        fileobj.close()
        raise
    archive.fileobj = fileobj
    return archive


def _open(fileobj) -> Archive:
    try:
        tar = tarfile.open(fileobj=fileobj, mode="r:")
        members = []
        for info in tar:
            members.append(info)
            if len(members) > MAX_MEMBERS:
                raise UnsafeArchive("the archive has more entries than the manager accepts")
    except (tarfile.TarError, EOFError, OSError) as err:
        raise UnsafeArchive(f"not a readable .tar.gz: {err}") from None
    comment = (tar.pax_headers or {}).get("comment", "")
    sha = comment if names.SHA_RE.fullmatch(comment or "") else None
    cleaned = [(_clean(m.name), m) for m in members]
    tops = {c.split("/", 1)[0] for c, _ in cleaned}
    if len(tops) != 1:
        raise UnsafeArchive("the archive does not have exactly one top folder")
    top = tops.pop()
    archive = Archive(sha=sha, top=top, tar=tar)
    total = 0
    links: dict[str, str] = {}
    for full, info in cleaned:
        if full == top:
            if not info.isdir():
                raise UnsafeArchive("the top entry is not a folder")
            continue
        rel = full[len(top) + 1:]
        if info.isdir():
            archive.dirs.append(rel)
        elif info.isreg():
            if info.size > MAX_FILE:
                raise UnsafeArchive(f"{show(rel)} is larger than the manager accepts")
            total += info.size
            if total > MAX_TOTAL:
                raise UnsafeArchive("the archive unpacks to more than the manager accepts")
            if rel in archive.files:
                raise UnsafeArchive(f"{show(rel)} is in the archive twice")
            archive.files[rel] = info
        elif info.issym():
            target = info.linkname
            if not target or target.startswith("/") or "\0" in target or "\\" in target:
                archive.skipped.append(rel)
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
                archive.skipped.append(rel)
                continue
            links[rel] = resolved
        else:
            raise UnsafeArchive(f"{show(rel)}: hard links, devices and FIFOs are refused")
    for rel, resolved in links.items():
        if resolved in archive.files:
            archive.links[rel] = resolved
        else:
            archive.skipped.append(rel)
    _check_names(archive)
    return archive


def _check_names(archive: Archive) -> None:
    """One name set across the kinds that are written: a name that is two of a file, a link and a folder, or a name
    below a file or a link, would fail midway through the extraction (or write through the link): refused here."""
    kinds: dict[str, str] = {rel: "file" for rel in archive.files}
    for rel in archive.links:
        if rel in kinds:
            raise UnsafeArchive(f"{show(rel)} is in the archive as a {kinds[rel]} and a link")
        kinds[rel] = "link"
    for rel in archive.dirs:
        if kinds.get(rel, "folder") != "folder":
            raise UnsafeArchive(f"{show(rel)} is in the archive as a {kinds[rel]} and a folder")
        kinds[rel] = "folder"
    for rel in kinds:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if kinds.get(parent, "folder") != "folder":
                raise UnsafeArchive(f"{show(rel)} is below {show(parent)}, a {kinds[parent]} of the archive")


def extract(archive: Archive, dest: str) -> None:
    """Write the whole tree below ``dest`` (an empty folder the caller created)."""
    for rel in sorted(archive.dirs):
        children.make_dirs(dest, rel)
    for rel, info in archive.files.items():
        children.write_file(dest, rel, archive.read(rel), 0o755 if info.mode & 0o100 else 0o644)
    for rel, resolved in archive.links.items():
        children.make_link(dest, rel, posixpath.relpath(resolved, posixpath.dirname(rel) or "."))
