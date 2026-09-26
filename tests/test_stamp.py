"""Stamping HRI's app template (the v0.25.0 fixture) into an instance's definition, and the two ways of filling an
instance folder: a release (the template and app files) and a git build (the whole tree, with no second config.*)."""

import os
import unittest

import yaml

from hrimgr import names, stamp, tarsafe

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

    def test_the_yaml_is_safe_and_round_trips(self):
        out = stamp.stamp(template(), "garage", "0.25.0", "release")
        raw = stamp.dump(out, "test")
        self.assertNotIn(b"!!python", raw)
        self.assertTrue(raw.startswith(b"# Written by HRI Manager"))
        self.assertEqual(yaml.safe_load(raw), out)
        self.assertEqual(yaml.safe_load(stamp.dump({"version": "1.0", "x": "yes", "n": "null"}, "t")),
                         {"version": "1.0", "x": "yes", "n": "null"})  # strings stay strings


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
        for kept in ("custom_components/integration_manager/static/config.js",
                     "custom_components/integration_manager/static/config.css",
                     "custom_components/integration_manager/templates/config.html", "DOCS.md", "translations/en.yaml",
                     "README.md", "app/DOCS.md"):
            with self.subTest(kept=kept):
                self.assertTrue(os.path.isfile(os.path.join(dest, kept)))
        for gone in ("app/config.yaml", "tests/e2e/config.yaml", "tests/e2e/config.json", "repository.yaml", ".github/haos/config.yaml"):
            with self.subTest(gone=gone):
                self.assertFalse(os.path.exists(os.path.join(dest, gone)))
        self.assertNotIn("image", config)
        with open(os.path.join(dest, "Dockerfile"), encoding="utf-8") as fh:
            self.assertIn(f"ARG HRI_BUILD={sha}\n", fh.read())
        self.assertIn("removed app/config.yaml", notes)

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
                    "d/config.yml", "e/configs.yaml"):
            os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").close()
        self.assertEqual(stamp.find_configs(root), ["a/config.json", "config.yaml", "d/config.yml"])


if __name__ == "__main__":
    unittest.main()
