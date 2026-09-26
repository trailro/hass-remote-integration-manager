"""Stamping HRI's app template (the v0.25.0 fixture) into an instance's definition, and the two ways of filling an
instance folder: a release (the template and app files) and a git build (the whole tree, with no second config.*)."""

import os
import time
import unittest

import yaml

from hrimgr import copies, names, stamp, tarsafe

from .fakes.tarballs import hri_files, make_tarball, sha_of
from .helpers import FIXTURE_CONFIG, tmpdir


def template() -> dict:
    return stamp.parse_template(FIXTURE_CONFIG.read_bytes())


class StampTest(unittest.TestCase):
    def test_the_fixture_is_hri_0_25_0(self):
        t = template()
        self.assertEqual(t["slug"], "hass_remote_integration")
        self.assertEqual(t["version"], "0.24.0")  # at the tag, the template still names the previous version
        self.assertEqual(t["image"], "ghcr.io/trailro/hass-remote-integration")

    def test_release_stamp(self):
        t = template()
        out = stamp.stamp(t, "garage", "0.25.0", "release")
        self.assertEqual(out["slug"], "hri_garage")
        self.assertEqual(out["name"], "HRI Garage")
        self.assertEqual(out["panel_title"], "HRI garage")
        self.assertEqual(out["version"], "0.25.0")
        self.assertEqual(out["ports"], {"8087/tcp": None})
        self.assertEqual(out["image"], t["image"])
        self.assertNotIn("webui", out)
        self.assertEqual(len(out["backup_exclude"]), len(t["backup_exclude"]))
        for before, after in zip(t["backup_exclude"], out["backup_exclude"]):
            if before.startswith("*_hass_remote_integration/"):
                self.assertEqual(after, "*_hri_garage/" + before[len("*_hass_remote_integration/"):])
            else:
                self.assertEqual(after, before)
        self.assertFalse(any("hass_remote_integration" in e for e in out["backup_exclude"]))
        changed = {"slug", "name", "panel_title", "version", "ports", "backup_exclude"}
        for key in t:
            if key not in changed:
                with self.subTest(key=key):
                    self.assertEqual(out[key], t[key])
        self.assertEqual(list(out), list(t))  # the order of the keys too

    def test_git_stamp_drops_the_image(self):
        out = stamp.stamp(template(), "garage", "0.0.0-abcdef1", "git")
        self.assertNotIn("image", out)
        self.assertEqual(out["version"], "0.0.0-abcdef1")

    def test_webui_is_dropped(self):
        t = template()
        t["webui"] = "http://[HOST]:[PORT:8087]/"
        self.assertNotIn("webui", stamp.stamp(t, "garage", "0.25.0", "release"))

    def test_the_template_must_have_what_is_stamped(self):
        for key in stamp.STAMPED_KEYS:
            t = template()
            del t[key]
            with self.subTest(key=key), self.assertRaises(stamp.TemplateError):
                stamp.parse_template(yaml.safe_dump(t).encode())
        bad = [b"- a list\n", b"slug: [\n", b"\xff\xfe", yaml.safe_dump({**template(), "slug": "other"}).encode(),
               yaml.safe_dump({**template(), "ports": [8087]}).encode(), b"x" * (stamp.MAX_TEMPLATE + 1)]
        for raw in bad:
            with self.subTest(raw=raw[:20]), self.assertRaises(stamp.TemplateError):
                stamp.parse_template(raw)

    def test_the_template_may_hold_only_the_vetted_keys(self):
        """Both channels: a key HRI's template does not have, or one that gives more than HRI's app has, refuses the
        whole definition instead of passing through to an app the manager writes with its role."""
        for key, value in (("hassio_role", "admin"), ("hassio_api", True), ("full_access", True), ("docker_api", True),
                           ("host_network", True), ("host_pid", True), ("host_dbus", True), ("privileged", ["SYS_ADMIN"]),
                           ("devices", ["/dev/mem"]), ("apparmor", False), ("auth_api", True), ("homeassistant_api", True),
                           ("kernel_modules", True), ("udev", True), ("usb", True), ("gpio", True), ("audio", True),
                           ("video", True), ("environment", {"X": "1"}), ("init", False), ("stdin", True),
                           ("tmpfs", True), ("discovery", ["mqtt"]), ("services", ["mqtt:need"]), ("realtime", True),
                           ("journald", True), ("backup", "cold"), ("startup", "system")):
            t = template()
            t[key] = value
            with self.subTest(key=key), self.assertRaises(stamp.TemplateError) as ctx:
                stamp.parse_template(yaml.safe_dump(t).encode())
            self.assertIn(f"has '{key}', which this manager version does not accept; update the manager", str(ctx.exception))

    def test_values_outside_the_vetted_range_are_refused(self):
        for key, value in (("map", [{"type": "homeassistant_config", "read_only": False}]),
                           ("map", [{"type": "app_config", "read_only": False}, {"type": "backup"}]),
                           ("map", ["config:rw"]), ("map", [{"type": "app_config", "path": "/"}]),
                           ("image", "ghcr.io/someone/else"), ("url", "https://example.com/fork"),
                           ("schema", {"port": "device(subsystem=tty)"}), ("schema", {"x": "device"}),
                           ("schema", {"x": {"nested": "str"}}), ("options", {"x": {"nested": 1}}),
                           ("ingress", False), ("ingress_port", 0), ("timeout", 1000), ("arch", ["amd64", "mips"]),
                           ("arch", []), ("uart", "yes"), ("panel_icon", "javascript:alert(1)"),
                           ("ports", {"8087/tcp": "8087"}), ("ports", {"8087": 8087}), ("ports_description", {"x": "y"})):
            t = template()
            t[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(stamp.TemplateError) as ctx:
                stamp.parse_template(yaml.safe_dump(t).encode())
            self.assertIn("update the manager", str(ctx.exception))

    def test_hri_s_template_is_accepted(self):
        """HRI's template at v0.25.0; at the time of writing HRI's main has the same keys (only version differs)."""
        t = template()
        stamp.vet_template(t)
        self.assertEqual(set(t) - set(stamp.TEMPLATE_KEYS), set())
        for value in ({"type": "app_config"}, {"type": "app_config", "read_only": True}):
            stamp.vet_template({**t, "map": [value], "uart": False, "ports": {"8087/tcp": None}})

    def test_a_git_tree_with_a_privileged_template_is_refused(self):
        files = hri_files()
        files["app/config.yaml"] += b"\nfull_access: true\n"
        archive = tarsafe.open_archive(make_tarball("hass-remote-integration-x", files, sha_of("x")))
        for build in (lambda d: stamp.build_git(archive, d, "garage", "0.0.0-abc", "a" * 40, "src"),
                      lambda d: stamp.build_release(archive, d, "garage", "0.25.0", "src")):
            dest = tmpdir(self)
            with self.assertRaises(stamp.TemplateError):
                build(dest)
            self.assertEqual(os.listdir(dest), [])

    def test_a_slug_left_elsewhere_in_backup_exclude_is_refused(self):
        t = template()
        t["backup_exclude"] = t["backup_exclude"] + ["*/x_hass_remote_integration/y"]
        with self.assertRaises(stamp.TemplateError):
            stamp.stamp(t, "garage", "0.25.0", "release")

    def test_the_header_is_one_line(self):
        config = stamp.stamp(stamp.parse_template(FIXTURE_CONFIG.read_bytes()), "garage", "0.25.0", "release")
        for source in ("a\nprivileged: true", "a\rb", "a\r\nb"):
            with self.subTest(source=source), self.assertRaises(stamp.TemplateError):
                stamp.dump(config, source)

    def test_the_yaml_is_safe_and_round_trips(self):
        out = stamp.stamp(template(), "garage", "0.25.0", "release")
        raw = stamp.dump(out, "test")
        self.assertNotIn(b"!!python", raw)
        self.assertTrue(raw.startswith(b"# Written by HRI Manager"))
        self.assertEqual(yaml.safe_load(raw), out)
        self.assertEqual(yaml.safe_load(stamp.dump({"version": "1.0", "x": "yes", "n": "null"}, "t")),
                         {"version": "1.0", "x": "yes", "n": "null"})  # strings stay strings


def alias_bomb(levels: int) -> str:
    """A YAML sequence of a few hundred bytes whose last item spells out as 2**levels items (nested aliases)."""
    return "[" + ", ".join(["&a0 [x, x]"] + [f"&a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, levels)]) + "]"


class AliasTest(unittest.TestCase):
    """A refusal never spells a value out: str() of nested aliases takes time and memory doubling per level, and the
    template (HRI upstream) and the copy in /data (from a folder others can write) are both read with aliases."""

    LEVELS = 25  # about 30 million items spelled out; a refusal must not notice

    def refused_quickly(self, call) -> Exception:
        start = time.monotonic()
        with self.assertRaises(Exception) as ctx:
            call()
        self.assertLess(time.monotonic() - start, 0.1)
        return ctx.exception

    def test_a_template_with_a_nested_alias_map_is_refused_at_once(self):
        doc = yaml.safe_dump({k: v for k, v in template().items() if k != "map"}) + f"map: {alias_bomb(self.LEVELS)}\n"
        err = self.refused_quickly(lambda: stamp.parse_template(doc.encode()))
        self.assertIsInstance(err, stamp.TemplateError)
        self.assertIn("map: a list value", str(err))
        err = self.refused_quickly(lambda: stamp.vet_template({"map": yaml.safe_load(alias_bomb(self.LEVELS))}))
        self.assertIsInstance(err, stamp.TemplateError)

    def test_a_copy_with_a_nested_alias_is_refused_at_once(self):
        config = yaml.safe_load(stamp.dump(stamp.stamp(template(), "garage", "0.25.0", "release"), "t"))
        for key in ("map", "version", "arch", "options"):
            bomb = {**config, key: yaml.safe_load(alias_bomb(self.LEVELS))}
            with self.subTest(key=key):
                err = self.refused_quickly(lambda: copies.check(bomb, "garage", "0.25.0", "release"))
                self.assertIsInstance(err, copies.CopyError)


class CopySaveTest(unittest.TestCase):
    """The copy in /data is taken from the instance's folder, which others can write: only a definition this manager
    writes is kept."""

    def folder(self, config: bytes) -> str:
        path = os.path.join(tmpdir(self), "hri_garage")
        os.makedirs(os.path.join(path, "translations"))
        with open(os.path.join(path, "config.yaml"), "wb") as fh:
            fh.write(config)
        with open(os.path.join(path, "translations", "en.yaml"), "wb") as fh:
            fh.write(b"configuration: {}\n")
        return path

    def save(self, config: bytes):
        root = tmpdir(self)
        marker = {"name": "garage", "instance_id": "a" * 32, "channel": "release", "version": "0.25.0",
                  "template_source": "https://codeload.github.com/x"}
        return root, lambda: copies.save(root, "garage", self.folder(config), marker)

    def test_what_the_manager_writes_is_kept(self):
        root, save = self.save(stamp.dump(stamp.stamp(template(), "garage", "0.25.0", "release"), "t"))
        self.assertEqual(save(), ["config.yaml", "translations/en.yaml"])
        self.assertTrue(os.path.isdir(os.path.join(root, "garage")))

    def test_a_changed_definition_is_not_kept(self):
        stamped = stamp.dump(stamp.stamp(template(), "garage", "0.25.0", "release"), "t")
        for what, config in (("a privilege", stamped + b"full_access: true\n"),
                             ("a nested alias", stamped + f"map: {alias_bomb(25)}\n".encode()),  # the last map: counts
                             ("not YAML", b"slug: [\n")):
            with self.subTest(what=what):
                root, save = self.save(config)
                start = time.monotonic()
                with self.assertRaises(copies.CopyError):
                    save()
                self.assertLess(time.monotonic() - start, 0.1)
                self.assertEqual(os.listdir(root), [])


class BuildTest(unittest.TestCase):
    def archive(self, files=None, sha=None):
        return tarsafe.open_archive(make_tarball("hass-remote-integration-0.25.0", files or hri_files(), sha or sha_of("x")))

    def test_release_folder(self):
        dest = tmpdir(self)
        stamp.build_release(self.archive(), dest, "garage", "0.25.0", "https://codeload.example/v0.25.0")
        found = sorted(os.path.relpath(os.path.join(d, f), dest) for d, _, fs in os.walk(dest) for f in fs)
        self.assertEqual(found, ["CHANGELOG.md", "DOCS.md", "config.yaml", "translations/en.yaml"])
        with open(os.path.join(dest, "config.yaml"), encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
        self.assertEqual((config["slug"], config["version"]), ("hri_garage", "0.25.0"))
        self.assertEqual(stamp.find_configs(dest), ["config.yaml"])

    def test_git_folder_has_one_app_and_keeps_config_assets(self):
        dest = tmpdir(self)
        sha = sha_of("main")
        config, notes = stamp.build_git(self.archive(sha=sha), dest, "garage", names.git_version(sha), sha, "src")
        self.assertEqual(stamp.find_configs(dest), ["config.yaml"])
        # what the Supervisor does not read as an app stays: the build may need it
        for kept in ("custom_components/integration_manager/static/config.js",
                     "custom_components/integration_manager/static/config.css",
                     "custom_components/integration_manager/templates/config.html", "DOCS.md", "translations/en.yaml",
                     "README.md", "app/DOCS.md", "repository.yaml", ".github/haos/config.yaml", "rootfs/etc/config.yaml",
                     "tools/Dockerfile.dev"):
            with self.subTest(kept=kept):
                self.assertTrue(os.path.isfile(os.path.join(dest, kept)))
        for gone in ("app/config.yaml", "tests/e2e/config.yaml", "tests/e2e/config.json", "docs/config.example.yaml"):
            with self.subTest(gone=gone):
                self.assertFalse(os.path.exists(os.path.join(dest, gone)))
        self.assertNotIn("image", config)
        with open(os.path.join(dest, "Dockerfile"), encoding="utf-8") as fh:
            self.assertIn(f"ARG HRI_BUILD={sha}\n", fh.read())
        self.assertIn("removed 'app/config.yaml'", notes)

    def test_git_trees_with_build_files_at_their_root_are_refused(self):
        """apparmor.txt replaces the default AppArmor profile, build.* sets base images and arguments, a
        Dockerfile.<arch> is built instead of the Dockerfile the manager patches."""
        for extra in ("apparmor.txt", "build.yaml", "build.yml", "build.json", "Dockerfile.amd64", "Dockerfile.aarch64",
                      "Dockerfile.x"):
            files = hri_files()
            files[extra] = b"x\n"
            dest = tmpdir(self)
            with self.subTest(extra=extra), self.assertRaises(stamp.TemplateError) as ctx:
                stamp.build_git(self.archive(files), dest, "garage", "0.0.0-abc", "a" * 40, "src")
            self.assertIn(extra, str(ctx.exception))
            self.assertEqual(os.listdir(dest), [])
        files = hri_files()
        files.update({"docs/apparmor.txt": b"x", "sub/build.yaml": b"x"})  # below the root the Supervisor does not look
        stamp.build_git(self.archive(files), tmpdir(self), "garage", "0.0.0-abc", "a" * 40, "src")

    def test_git_without_a_dockerfile_is_refused(self):
        files = hri_files()
        del files["Dockerfile"]
        with self.assertRaises(stamp.TemplateError):
            stamp.build_git(self.archive(files), tmpdir(self), "garage", "0.0.0-abc", "a" * 40, "src")

    def test_no_app_template_is_refused(self):
        files = hri_files()
        del files["app/config.yaml"]
        with self.assertRaises(stamp.TemplateError):
            stamp.build_release(self.archive(files), tmpdir(self), "garage", "0.25.0", "src")

    def test_find_configs_follows_the_supervisor(self):
        root = tmpdir(self)
        for rel in ("config.yaml", "a/config.json", "a/config.js", ".hidden/config.yaml", "b/rootfs/config.yaml", "c/.x/config.yml",
                    "d/config.yml", "e/configs.yaml", "f/config.example.yaml", "f/config.x.json", "g/Config.yaml",
                    "g/config.", "g/config.yaml.bak", "rootfs/config.yml", "h/.config.yaml"):
            os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").close()
        self.assertEqual(stamp.find_configs(root), ["a/config.json", "config.yaml", "d/config.yml", "f/config.example.yaml",
                                                    "f/config.x.json"])


if __name__ == "__main__":
    unittest.main()
