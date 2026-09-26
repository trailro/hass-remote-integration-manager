"""Instance folders and markers: what counts as the manager's, and writes that stay inside hri_<name>/, atomically."""

import json
import os
import unittest

from hrimgr import children

from hrimgr.registry import Registry

from .helpers import make_child, marker, new_registry, register, tmpdir


def build_with(files, marker_data):
    def build(tmp):
        for rel, data in files.items():
            children.write_file(tmp, rel, data)
        return marker_data
    return build


class MarkerTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.reg = new_registry(self)

    def test_a_valid_marker(self):
        make_child(self.root, "garage", registry=self.reg)
        m = children.load_managed(self.root, "garage", self.reg)
        self.assertEqual(m.slug, "local_hri_garage")
        m.verify()

    def test_marker_and_registry_must_agree(self):
        make_child(self.root, "garage")  # a marker nobody registered: forged, or the registry was reset
        with self.assertRaises(children.NotManaged) as ctx:
            children.load_managed(self.root, "garage", self.reg)
        self.assertIn("registry has no instance garage", str(ctx.exception))
        with self.assertRaises(children.NotManaged):
            children.load_managed(self.root, "garage", None)
        for key, value in (("instance_id", "f" * 32), ("slug", "local_hri_other"), ("channel", "git")):
            register(self.reg, marker("garage"))
            self.reg.update("garage", **{key: value})
            with self.subTest(key=key), self.assertRaises(children.NotManaged):
                children.load_managed(self.root, "garage", self.reg)
        register(self.reg, marker("garage"))
        children.load_managed(self.root, "garage", self.reg)
        with open(self.reg.path, "w") as fh:
            fh.write("not json")
        with self.assertRaises(children.NotManaged):  # an unreadable registry: nothing is the manager's
            children.load_managed(self.root, "garage", self.reg)

    def test_the_registry(self):
        reg = Registry(os.path.join(tmpdir(self), "instances.json"))
        self.assertEqual(reg.all(), {})
        reg.put("garage", {"name": "garage", "instance_id": "a" * 32})
        self.assertEqual(reg.update("garage", setup_complete=True)["setup_complete"], True)
        self.assertEqual(Registry(reg.path).get("garage"), {"name": "garage", "instance_id": "a" * 32, "setup_complete": True})
        self.assertEqual(oct(os.stat(reg.path).st_mode & 0o777), "0o600")
        reg.remove("garage")
        self.assertIsNone(reg.get("garage"))

    def test_what_is_not_managed(self):
        cases = {
            "nomarker": False,
            "othermgr": marker("othermgr", manager="someone"),
            "wrongname": marker("other"),
            "wrongslug": marker("wrongslug", slug="core_ssh"),
            "badchannel": marker("badchannel", channel="nightly"),
            "noid": marker("noid", instance_id=None),
            "shortid": marker("shortid", instance_id="abc"),
            "notobject": ["x"],
        }
        for name, data in cases.items():
            make_child(self.root, name, data, registry=self.reg)
            with self.subTest(name=name), self.assertRaises(children.NotManaged):
                children.load_managed(self.root, name, self.reg)
        with self.assertRaises(children.NotManaged):
            children.load_managed(self.root, "missing", self.reg)

    def test_a_marker_that_is_not_json_or_too_large(self):
        folder = make_child(self.root, "garbage", False)
        with open(os.path.join(folder, children.MARKER), "wb") as fh:
            fh.write(b"\xff{")
        with self.assertRaises(children.NotManaged):
            children.load_managed(self.root, "garbage", self.reg)
        folder = make_child(self.root, "huge", False)
        with open(os.path.join(folder, children.MARKER), "w") as fh:
            json.dump({**marker("huge"), "pad": "x" * children.MAX_MARKER}, fh)
        with self.assertRaises(children.NotManaged):
            children.load_managed(self.root, "huge", self.reg)

    def test_links_are_not_followed(self):
        real = make_child(tmpdir(self), "garage", registry=self.reg)
        os.symlink(real, os.path.join(self.root, "hri_garage"))
        with self.assertRaises(children.NotManaged):
            children.load_managed(self.root, "garage", self.reg)
        folder = make_child(self.root, "linked", False)
        elsewhere = os.path.join(tmpdir(self), "m.json")
        with open(elsewhere, "w") as fh:
            json.dump(marker("linked"), fh)
        register(self.reg, marker("linked"))
        os.symlink(elsewhere, os.path.join(folder, children.MARKER))
        with self.assertRaises(children.NotManaged):
            children.load_managed(self.root, "linked", self.reg)

    def test_scan(self):
        make_child(self.root, "garage", registry=self.reg)
        make_child(self.root, "foreign", False)
        make_child(self.root, "forged")  # a marker without the registry's entry: not managed
        os.makedirs(os.path.join(self.root, "my_own_app"))
        os.makedirs(os.path.join(self.root, "hri_Bad-Name"))
        found = {name: (m is not None, problem is None) for name, m, problem in children.scan(self.root, self.reg)}
        self.assertEqual(found, {"garage": (True, True), "foreign": (False, False), "forged": (False, False)})


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.reg = new_registry(self)
        for name in ("garage", "attic"):
            register(self.reg, marker(name))

    def entries(self):
        return sorted(os.listdir(self.root))

    def test_write_new(self):
        m = children.write_new(self.root, "garage", build_with({"config.yaml": b"x", "translations/en.yaml": b"y"}, marker("garage")), self.reg)
        self.assertEqual(self.entries(), ["hri_garage"])
        self.assertEqual(m.marker["name"], "garage")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "hri_garage", "translations", "en.yaml")))

    def test_a_failed_build_leaves_nothing(self):
        def boom(tmp):
            children.write_file(tmp, "config.yaml", b"x")
            raise RuntimeError("download failed")
        with self.assertRaises(RuntimeError):
            children.write_new(self.root, "garage", boom, self.reg)
        self.assertEqual(self.entries(), [])
        with self.assertRaises(children.NotManaged):  # a build returning another instance's marker
            children.write_new(self.root, "garage", build_with({}, marker("attic")), self.reg)
        self.assertEqual(self.entries(), [])

    def test_an_existing_folder_is_never_overwritten(self):
        make_child(self.root, "garage", False)
        with self.assertRaises(children.UnsafePath):
            children.write_new(self.root, "garage", build_with({}, marker("garage")), self.reg)
        os.symlink("/", os.path.join(self.root, "hri_attic"))
        with self.assertRaises(children.UnsafePath):
            children.write_new(self.root, "attic", build_with({}, marker("attic")), self.reg)

    def test_paths_stay_inside(self):
        base = tmpdir(self)
        for rel in ("../x", "/etc/x", "a/../../x", "a//b", "", ".", "a\\b"):
            with self.subTest(rel=rel), self.assertRaises(children.UnsafePath):
                children.write_file(base, rel, b"x")
        os.symlink(tmpdir(self), os.path.join(base, "link"))
        with self.assertRaises(children.UnsafePath):
            children.write_file(base, "link/x", b"x")
        with open(os.path.join(base, "f"), "w"):
            pass
        os.symlink(os.path.join(base, "f"), os.path.join(base, "flink"))
        with self.assertRaises(OSError):
            children.write_file(base, "flink", b"x")

    def test_replace_commit_and_rollback(self):
        m = children.write_new(self.root, "garage", build_with({"config.yaml": b"old"}, marker("garage")), self.reg)
        r = children.replace(m, build_with({"config.yaml": b"new"}, marker("garage", version="0.25.1")))
        with open(os.path.join(self.root, "hri_garage", "config.yaml"), "rb") as fh:
            self.assertEqual(fh.read(), b"new")
        r.rollback()
        with open(os.path.join(self.root, "hri_garage", "config.yaml"), "rb") as fh:
            self.assertEqual(fh.read(), b"old")
        self.assertEqual(self.entries(), ["hri_garage"])
        r = children.replace(children.load_managed(self.root, "garage", self.reg), build_with({"config.yaml": b"new"}, marker("garage")))
        r.commit()
        self.assertEqual(self.entries(), ["hri_garage"])

    def test_replace_and_remove_need_the_marker(self):
        m = children.write_new(self.root, "garage", build_with({}, marker("garage")), self.reg)
        os.unlink(os.path.join(self.root, "hri_garage", children.MARKER))
        with self.assertRaises(children.NotManaged):
            children.replace(m, build_with({}, marker("garage")))
        with self.assertRaises(children.NotManaged):
            children.remove(m)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "hri_garage")))

    def test_remove(self):
        m = children.write_new(self.root, "garage", build_with({"a/b": b"x"}, marker("garage")), self.reg)
        children.remove(m)
        self.assertEqual(self.entries(), [])

    def test_cleanup_after_a_crash(self):
        os.makedirs(os.path.join(self.root, ".hri-tmp-garage-0123abcd"))
        os.makedirs(os.path.join(self.root, ".hri-del-attic-0123abcd"))
        os.makedirs(os.path.join(self.root, ".hri-old-cellar-0123abcd"))  # the only definition left: put back
        make_child(self.root, "porch")
        os.makedirs(os.path.join(self.root, ".hri-old-porch-0123abcd"))  # the new one is in place: goes
        os.makedirs(os.path.join(self.root, ".other-tool"))
        done = children.cleanup_stale(self.root)
        self.assertEqual(self.entries(), [".other-tool", "hri_cellar", "hri_porch"])
        self.assertEqual(len(done), 4)


if __name__ == "__main__":
    unittest.main()
