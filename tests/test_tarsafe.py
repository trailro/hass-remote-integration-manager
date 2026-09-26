"""A source tarball is checked before anything is written: names, member types, links, counts and sizes."""

import io
import os
import tarfile
import time
import unittest
from unittest import mock

from hrimgr import tarsafe

from .fakes.tarballs import make_tarball, sha_of
from .helpers import tmpdir

TOP = "hass-remote-integration-main"


def member(name, kind=tarfile.REGTYPE, data=b"", linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.linkname = linkname
    info.mode = 0o644
    return (info, data if kind == tarfile.REGTYPE else None)


class TarSafeTest(unittest.TestCase):
    def test_a_normal_archive(self):
        sha = sha_of("x")
        archive = tarsafe.open_archive(make_tarball(TOP, {"a.txt": b"a", "d/b.txt": b"bb"}, sha))
        self.assertEqual(archive.sha, sha)
        self.assertEqual(archive.top, TOP)
        self.assertEqual(sorted(archive.files), ["a.txt", "d/b.txt"])
        self.assertEqual(archive.read("d/b.txt"), b"bb")

    def test_no_sha_without_the_pax_comment(self):
        self.assertIsNone(tarsafe.open_archive(make_tarball(TOP, {"a": b"a"})).sha)
        self.assertIsNone(tarsafe.open_archive(make_tarball(TOP, {"a": b"a"}, "not-a-sha")).sha)

    def test_refused_names(self):
        for name in ("/etc/passwd", f"{TOP}/../escape", f"{TOP}/a/../../escape", "../escape", f"{TOP}/a\\b", f"{TOP}/\0x"):
            with self.subTest(name=name):
                data = make_tarball(TOP, {"ok": b"1"}, extra=[member(name, data=b"x")])
                with self.assertRaises(tarsafe.UnsafeArchive):
                    tarsafe.open_archive(data)

    def test_two_top_folders_are_refused(self):
        with self.assertRaises(tarsafe.UnsafeArchive):
            tarsafe.open_archive(make_tarball(TOP, {"a": b"1"}, extra=[member("other/x", data=b"x")]))

    def test_hard_links_devices_and_fifos_refuse_the_archive(self):
        for kind in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                data = make_tarball(TOP, {"a": b"1"}, extra=[member(f"{TOP}/x", kind, linkname=f"{TOP}/a")])
                with self.assertRaises(tarsafe.UnsafeArchive):
                    tarsafe.open_archive(data)

    def test_symlinks(self):
        extra = [
            member(f"{TOP}/inside", tarfile.SYMTYPE, linkname="d/file"),
            member(f"{TOP}/d/up", tarfile.SYMTYPE, linkname="../d/file"),
            member(f"{TOP}/abs", tarfile.SYMTYPE, linkname="/etc/passwd"),
            member(f"{TOP}/out", tarfile.SYMTYPE, linkname="../../etc/passwd"),
            member(f"{TOP}/todir", tarfile.SYMTYPE, linkname="d"),
            member(f"{TOP}/dangling", tarfile.SYMTYPE, linkname="nothing"),
        ]
        archive = tarsafe.open_archive(make_tarball(TOP, {"d/file": b"f"}, extra=extra))
        self.assertEqual(archive.links, {"inside": "d/file", "d/up": "d/file"})
        self.assertEqual(sorted(archive.skipped), ["abs", "dangling", "out", "todir"])
        dest = tmpdir(self)
        tarsafe.extract(archive, dest)
        self.assertEqual(os.readlink(os.path.join(dest, "inside")), "d/file")
        with open(os.path.join(dest, "d", "up"), "rb") as fh:
            self.assertEqual(fh.read(), b"f")
        self.assertFalse(os.path.lexists(os.path.join(dest, "abs")))
        self.assertFalse(os.path.lexists(os.path.join(dest, "out")))

    def test_caps(self):
        with mock.patch.object(tarsafe, "MAX_MEMBERS", 3), self.assertRaises(tarsafe.UnsafeArchive):
            tarsafe.open_archive(make_tarball(TOP, {str(i): b"" for i in range(5)}))
        with mock.patch.object(tarsafe, "MAX_FILE", 4), self.assertRaises(tarsafe.UnsafeArchive):
            tarsafe.open_archive(make_tarball(TOP, {"big": b"12345"}))
        with mock.patch.object(tarsafe, "MAX_TOTAL", 6), self.assertRaises(tarsafe.UnsafeArchive):
            tarsafe.open_archive(make_tarball(TOP, {"a": b"1234", "b": b"1234"}))
        with mock.patch.object(tarsafe, "MAX_COMPRESSED", 10), self.assertRaises(tarsafe.UnsafeArchive):
            tarsafe.open_archive(make_tarball(TOP, {"a": b"1"}))

    def test_not_a_tarball(self):
        for data in (b"", b"not gzip", io.BytesIO().getvalue()):
            with self.assertRaises(tarsafe.UnsafeArchive):
                tarsafe.open_archive(data)

    def test_written_files_are_dated_now(self):
        """The Supervisor sees a changed local app by the newest mtime in its folder: a file dated in the future by its
        archive would hide every later change."""
        future = time.time() + 10 * 365 * 86400
        archive = tarsafe.open_archive(make_tarball(TOP, {"a": b"1", "x/b.sh": b"#!/bin/sh"}, mtime=future))
        dest = tmpdir(self)
        tarsafe.extract(archive, dest)
        for rel in ("a", "x/b.sh"):
            self.assertLess(os.stat(os.path.join(dest, rel)).st_mtime, time.time() + 60)

    def test_extract_never_writes_outside(self):
        archive = tarsafe.open_archive(make_tarball(TOP, {"a/b/c.txt": b"c"}))
        dest = tmpdir(self)
        os.symlink("/", os.path.join(dest, "a"))  # a link planted where a folder will be written
        with self.assertRaises(Exception):
            tarsafe.extract(archive, dest)


if __name__ == "__main__":
    unittest.main()
