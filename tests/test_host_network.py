"""Host network, per instance: ``host_network: true`` and ``ingress_port: 0`` in the instance's definition, chosen when
it is created (or with an Update or Rebuild that changes its version), recorded in the manager's registry, and never
taken from HRI's template.  Only for an HRI that listens on the port the Supervisor gives it: a release from 0.26.0 on,
a git tree whose entrypoint.py says ``APP_DYNAMIC_PORT = True``.  The manager's checks expect it exactly when the
registry says so, as for Bluetooth (tests/test_bluetooth.py)."""

import asyncio
import json
import os
import shutil
import unittest
from unittest import mock

import yaml

from hrimgr import copies, names, stamp, tarsafe

from . import APP_DIR
from .env import Env
from .fakes.tarballs import hri_files, make_tarball, sha_of
from .helpers import FIXTURE_0252, FIXTURE_CONFIG, tmpdir


def template() -> dict:
    return stamp.parse_template(FIXTURE_CONFIG.read_bytes())


def stamped(host_network: bool, channel: str = "release", version: str = "0.26.0", bluetooth: bool = False) -> dict:
    return yaml.safe_load(stamp.dump(stamp.stamp(template(), "garage", version, channel, bluetooth,
                                                 host_network=host_network), "t"))


def archive(files: dict[str, bytes]) -> tarsafe.Archive:
    return tarsafe.open_archive(make_tarball("hass-remote-integration-x", files, sha_of("x")))


class StampTest(unittest.TestCase):
    def test_the_manager_adds_host_network_and_ingress_port_0_and_nothing_else(self):
        for channel, version in (("release", "0.26.0"), ("git", "0.0.0-0123456789ab")):
            with self.subTest(channel=channel):
                with_hn, without = stamped(True, channel, version), stamped(False, channel, version)
                self.assertIs(with_hn.pop("host_network"), True)
                self.assertEqual(with_hn.pop("ingress_port"), 0)
                self.assertEqual(without.pop("ingress_port"), 8087)  # HRI's own
                self.assertEqual(with_hn, without)
                self.assertNotIn("host_network", stamped(False, channel, version))
        both = stamped(True, bluetooth=True)
        self.assertEqual((both["host_dbus"], both["host_network"], both["ingress_port"]), (True, True, 0))

    def test_the_template_never_adds_it(self):
        for key, value in (("host_network", True), ("host_network", False), ("ingress_port", 0)):
            with self.subTest(key=key, value=value), self.assertRaises(stamp.TemplateError):
                stamp.parse_template(yaml.safe_dump({**template(), key: value}).encode())

    def test_not_with_a_port_the_supervisor_picks_dynamic_ports_from(self):
        """apps/validate.py refuses an app with ingress_port 0 that declares a port of 62000-65500."""
        t = {**template(), "ports": {"8087/tcp": 8087, "62000/tcp": None}}
        stamp.stamp(t, "garage", "0.26.0", "release")
        with self.assertRaises(stamp.TemplateError):
            stamp.stamp(t, "garage", "0.26.0", "release", host_network=True)

    def test_a_copy_has_it_exactly_when_the_registry_says_host_network(self):
        copies.check(stamped(True), "garage", "0.26.0", "release", host_network=True)
        copies.check(stamped(False), "garage", "0.26.0", "release", host_network=False)
        copies.check(stamped(True, bluetooth=True), "garage", "0.26.0", "release", True, host_network=True)
        without = stamped(False)
        for config, host_network in ((stamped(True), False), (without, True),
                                     ({**without, "host_network": True}, True),  # HRI's own port kept
                                     ({**without, "ingress_port": 0}, True), ({**without, "ingress_port": 0}, False),
                                     ({**stamped(True), "host_network": "yes"}, True),
                                     ({**stamped(True), "ingress_port": False}, True),
                                     ({**stamped(True), "ingress_port": 62001}, True),
                                     ({**without, "host_network": False}, False)):
            with self.subTest(config={k: config.get(k) for k in ("host_network", "ingress_port")},
                              host_network=host_network), self.assertRaises(copies.CopyError):
                copies.check(config, "garage", "0.26.0", "release", host_network=host_network)

    def test_the_supervisor_must_report_it_exactly_then(self):
        for host_network in (True, False):
            expected = stamp.expected_view(stamped(host_network), "local_hri_garage")
            self.assertIs(expected["host_network"], host_network)
            self.assertIn("host_network", stamp.STORE_VIEW)
            self.assertIn("host_network", stamp.INSTALLED_VIEW)
            # the ports as declared: the Supervisor still reports them (and publishes none on the host's network)
            self.assertEqual(expected["network"], ["8087/tcp"])
            self.assertNotIn("ingress_port", expected)

    def test_which_hri_reads_its_port(self):
        self.assertEqual([v for v in ("0.25.2", "0.26.0b1", "0.26.0rc2", "0.26.0", "0.26.1", "1.0.0")
                          if names.reads_dynamic_port(v)], ["0.26.0", "0.26.1", "1.0.0"])
        self.assertTrue(stamp.reads_dynamic_port(archive(hri_files(dynamic_port=True))))
        self.assertFalse(stamp.reads_dynamic_port(archive(hri_files())))
        without = {k: v for k, v in hri_files().items() if k != "entrypoint.py"}
        self.assertFalse(stamp.reads_dynamic_port(archive(without)))
        for text in (b"# APP_DYNAMIC_PORT = True\n", b"APP_DYNAMIC_PORT = False\n", b"x = 'APP_DYNAMIC_PORT = True'\n",
                     b"    APP_DYNAMIC_PORT = True\n", b"APP_DYNAMIC_PORT = True or False\n",
                     # a line of a multi-line string, which a search of the lines would take
                     b'"""HRI.\n\nAPP_DYNAMIC_PORT = True\n"""\n', b"X = '''\nAPP_DYNAMIC_PORT = True\n'''\n",
                     # not at the top level
                     b"if False:\n    APP_DYNAMIC_PORT = True\n", b"def f():\n    APP_DYNAMIC_PORT = True\n",
                     b"class C:\n    APP_DYNAMIC_PORT = True\n",
                     # the last assignment decides; a truthy value that is not True; a bare annotation
                     b"APP_DYNAMIC_PORT = True\nAPP_DYNAMIC_PORT = False\n", b"APP_DYNAMIC_PORT = 1\n",
                     b"APP_DYNAMIC_PORT = 'True'\n", b"APP_DYNAMIC_PORT: bool\n",
                     # not Python, not UTF-8, a NUL
                     b"APP_DYNAMIC_PORT = True\ndef (:\n", b"APP_DYNAMIC_PORT = True\nx = '\xff'\n",
                     b"APP_DYNAMIC_PORT = True\n\x00\n"):
            with self.subTest(text=text):
                self.assertFalse(stamp.reads_dynamic_port(archive({**without, "entrypoint.py": text})))
        for text in (b"APP_DYNAMIC_PORT = True\n", b"a = 1\nAPP_DYNAMIC_PORT = True  # the Supervisor's port\r\n",
                     b"APP_DYNAMIC_PORT: bool = True\n", b"APP_DYNAMIC_PORT = (\n    True\n)\n",
                     b'"""HRI."""\nimport os\n\nAPP_DYNAMIC_PORT = False\nAPP_DYNAMIC_PORT = True\n'):
            with self.subTest(text=text):
                self.assertTrue(stamp.reads_dynamic_port(archive({**without, "entrypoint.py": text})))
        # larger than the manager reads
        big = b"APP_DYNAMIC_PORT = True\n" + b"#" * stamp.MAX_DYNAMIC_PORT_FILE + b"\n"
        self.assertFalse(stamp.reads_dynamic_port(archive({**without, "entrypoint.py": big})))
        # in another folder than the tree's root: not HRI's entrypoint
        moved = {**without, "tools/entrypoint.py": b"APP_DYNAMIC_PORT = True\n"}
        self.assertFalse(stamp.reads_dynamic_port(archive(moved)))


class PageTest(unittest.TestCase):
    def test_the_page_offers_it_on_create_update_and_rebuild_and_shows_it(self):
        static = APP_DIR / "hrimgr" / "static"
        html, js = (static / "index.html").read_text(encoding="utf-8"), (static / "mgr.js").read_text(encoding="utf-8")
        for needle in ('id="c-hn"', 'id="cf-hn-row"', 'id="cf-hn"', "mDNS, SSDP and broadcast",
                       "unless the app has a password", "the sidebar panel keeps working"):
            self.assertIn(needle, html)
        self.assertIn("bluetooth, host_network}", js)  # the create form's body, both channels
        self.assertEqual(js.count("host_network: c.hostNetwork"), 2)  # Update and Rebuild
        self.assertEqual(js.count("hostNetwork: !!i.host_network"), 2)
        self.assertIn(">Host network</span>", js)  # the row's badge


class HostNetworkFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()
        self.env.stub.releases += ["0.26.0", "0.26.1", "0.26.2"]
        self.env.stub.hri_fixture = FIXTURE_0252  # HRI's newest template

    async def asyncTearDown(self):
        self.env.assert_only_allowed_calls(self)
        await self.env.close()

    def config(self, name="garage"):
        with open(os.path.join(self.env.local_apps, f"hri_{name}", "config.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def copy_config(self, name="garage"):
        with open(os.path.join(self.env.data, "definitions", name, "config.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    async def create(self, name="garage", **body):
        body = {"name": name, "channel": "release", "version": "0.26.0", **body}
        return await self.env.job(await self.env.send("POST", "/api/instances", body))

    async def update(self, name="garage", **body):
        return await self.env.job(await self.env.send("POST", f"/api/instances/{name}/update", body))

    async def row(self, name="garage"):
        _, data = await self.env.get("/api/instances")
        return next(i for i in data["instances"] if i["name"] == name)

    def installed_host_network(self, slug="local_hri_garage"):
        return self.env.stub.installed[slug]["definition"]["host_network"]

    def assert_on(self, name="garage"):
        config = self.config(name)
        self.assertEqual((config["host_network"], config["ingress_port"]), (True, 0))
        self.assertIs(self.env.registry.get(name)["host_network"], True)
        with open(os.path.join(self.env.local_apps, f"hri_{name}", ".hri-manager.json"), encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["host_network"], True)
        copy = self.copy_config(name)
        self.assertEqual((copy["host_network"], copy["ingress_port"]), (True, 0))

    def assert_off(self, name="garage"):
        config = self.config(name)
        self.assertNotIn("host_network", config)
        self.assertEqual(config["ingress_port"], 8087)
        self.assertIs(self.env.registry.get(name)["host_network"], False)
        self.assertNotIn("host_network", self.copy_config(name))

    async def test_created_with_host_network(self):
        job = await self.create(host_network=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on()
        self.assertIs(self.installed_host_network(), True)  # what the check after the install expected
        row = await self.row()
        self.assertEqual((row["host_network"], row["bluetooth"]), (True, False))
        self.assertTrue(any("with Host network: the instance gets the host's network (host_network)" in line["msg"]
                            for line in job["lines"]))

    async def test_created_without_host_network(self):
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_off()
        self.assertIs(self.installed_host_network(), False)
        self.assertIs((await self.row())["host_network"], False)

    async def test_created_with_both(self):
        job = await self.create(host_network=True, bluetooth=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on()
        self.assertIs(self.config()["host_dbus"], True)
        row = await self.row()
        self.assertEqual((row["host_network"], row["bluetooth"]), (True, True))

    async def test_not_for_a_release_older_than_0_26_0(self):
        env = self.env
        for version in ("0.25.1", "0.26.0b1"):
            with self.subTest(version=version):
                calls = len(env.stub.calls)
                status, body = await env.send("POST", "/api/instances",
                                              {"name": "garage", "version": version, "host_network": True})
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], f"Host network needs HRI 0.26.0 or newer, the first release that "
                                                f"listens on the port the Supervisor gives it: not {version}.")
                self.assertEqual(env.stub.calls[calls:], [])
        self.assertFalse(os.path.exists(os.path.join(env.local_apps, "hri_garage")))
        self.assertIsNone(env.registry.get("garage"))

    async def test_a_git_build_only_with_a_tree_that_reads_its_port(self):
        env = self.env
        job = await self.create("lab", channel="git", ref_kind="branch", ref="main", host_network=True)
        self.assertEqual(job["state"], "failed", job)
        self.assertIn(f"commit {sha_of('main-1')[:12]} of the branch main does not", job["error"])
        self.assertIn("APP_DYNAMIC_PORT = True", job["error"])
        self.assertFalse(os.path.exists(os.path.join(env.local_apps, "hri_lab")))
        self.assertIsNone(env.registry.get("lab"))
        self.assertEqual(env.changing_calls(), [])
        env.stub.dynamic_port_refs.add("main")
        job = await self.create("lab", channel="git", ref_kind="branch", ref="main", host_network=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on("lab")
        self.assertNotIn("image", self.config("lab"))

    async def test_the_install_is_checked_against_the_registry_s_choice(self):
        env = self.env
        env.stub.install_override["local_hri_garage"] = {"host_network": True}
        job = await self.create()  # without Host network, the Supervisor installed it on the host's network
        self.assertEqual(job["state"], "failed")
        self.assertIn("host_network: True, not False", job["error"])
        self.assertIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)
        env.stub.install_override["local_hri_attic"] = {"host_network": False}
        job = await self.create("attic", host_network=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("host_network: False, not True", job["error"])

    async def test_the_store_is_checked_before_the_install(self):
        env = self.env
        env.stub.store_override["local_hri_garage"] = {"host_network": True}
        job = await self.create()
        self.assertEqual(job["state"], "failed")
        self.assertIn("host_network: True, not False", job["error"])
        self.assertNotIn("local_hri_garage", env.stub.installed)

    async def test_turned_on_and_off_with_updates_to_newer_releases(self):
        env = self.env
        await self.create()
        job = await self.update(version="0.26.1", host_network=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on()
        self.assertIs(self.installed_host_network(), True)
        self.assertTrue(any("Host network on: with the host's network (host_network), from this version on" in line["msg"]
                            for line in job["lines"]))
        job = await self.update(version="0.26.1")  # nothing in the request, at the same version: unchanged
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(job["result"]["unchanged"])
        job = await self.update(version="0.26.2", host_network=False)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_off()
        self.assertIs(self.installed_host_network(), False)

    async def test_kept_on_by_an_update_that_does_not_name_it(self):
        await self.create(host_network=True)
        job = await self.update(version="0.26.1", bluetooth=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on()
        self.assertIs(self.config()["host_dbus"], True)

    async def test_an_update_to_an_older_hri_is_refused_with_it(self):
        env = self.env
        await self.create("old", version="0.25.0")
        before = self.config("old")
        job = await self.update("old", version="0.25.1", host_network=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("Host network needs HRI 0.26.0 or newer", job["error"])
        self.assertIn("not 0.25.1", job["error"])
        self.assertEqual(self.config("old"), before)
        self.assertIs(env.registry.get("old")["host_network"], False)
        self.assertEqual(env.stub.installed["local_hri_old"]["version"], "0.25.0")
        job = await self.update("old", version="0.26.0", host_network=True)  # the first release that reads its port
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on("old")

    async def test_an_instance_without_its_definition_is_updated_with_it_only_to_0_26_0_on(self):
        """Update of an instance whose folder is gone (written and installed at once): the same gate."""
        env = self.env
        await self.create("old", version="0.25.0")
        shutil.rmtree(os.path.join(env.local_apps, "hri_old"))
        await env.sv.reload_store()
        job = await self.update("old", version="0.25.1", host_network=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("Host network needs HRI 0.26.0 or newer", job["error"])
        self.assertFalse(os.path.exists(os.path.join(env.local_apps, "hri_old")))
        self.assertIs(env.registry.get("old")["host_network"], False)
        job = await self.update("old", version="0.26.0", host_network=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on("old")
        self.assertIs(self.installed_host_network("local_hri_old"), True)

    async def test_not_without_a_version_change(self):
        env = self.env
        await self.create()
        before = self.config()
        calls = len(env.stub.calls)
        job = await self.update(version="0.26.0", host_network=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("the Supervisor applies Host network (the host's network) only when the app's version changes",
                      job["error"])
        self.assertIn("create it again with Host network on", job["error"])
        self.assertEqual(self.config(), before)
        self.assertIs(env.registry.get("garage")["host_network"], False)
        self.assertEqual([c for c in env.stub.calls[calls:] if c[0] == "POST" and c[1] != "/store/reload"], [])

    async def test_a_rebuild_needs_a_tree_that_reads_its_port(self):
        """A Rebuild of a git instance with Host network onto a commit without APP_DYNAMIC_PORT is refused (nothing
        written); the same Rebuild with Host network off goes ahead."""
        env = self.env
        env.stub.dynamic_port_refs.add("main")
        job = await self.create("lab", channel="git", ref_kind="branch", ref="main", host_network=True)
        self.assertEqual(job["state"], "succeeded", job)
        before = self.config("lab")
        env.stub.refs["old"] = sha_of("old-1")  # a branch without it
        job = await self.update("lab", ref_kind="branch", ref="old")
        self.assertEqual(job["state"], "failed")
        self.assertIn("of the branch old does not", job["error"])
        self.assertIn("Rebuild with Host network off", job["error"])
        self.assertEqual(self.config("lab"), before)
        self.assertEqual(env.registry.get("lab")["ref"], "main")
        job = await self.update("lab", ref_kind="branch", ref="old", host_network=False)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_off("lab")
        self.assertEqual(env.registry.get("lab")["ref"], "old")
        self.assertIs(self.installed_host_network("local_hri_lab"), False)

    async def test_a_rebuild_turns_it_on_only_with_a_new_commit(self):
        env = self.env
        env.stub.dynamic_port_refs.add("main")
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        job = await self.update("lab", host_network=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("only when the app's version changes", job["error"])
        env.stub.refs["main"] = sha_of("main-2")
        job = await self.update("lab", host_network=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on("lab")

    async def test_repair_and_install_keep_it(self):
        env = self.env
        await self.create(host_network=True)
        folder = os.path.join(env.local_apps, "hri_garage")
        for source in ("the copy", "GitHub"):
            with self.subTest(source=source):
                shutil.rmtree(folder)
                if source == "GitHub":
                    shutil.rmtree(os.path.join(env.data, "definitions", "garage"))
                await env.sv.reload_store()
                job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
                self.assertEqual(job["state"], "succeeded", job)
                self.assert_on()
        del env.stub.installed["local_hri_garage"]  # a definition without its app: Install checks it
        job = await env.job(await env.send("POST", "/api/instances/garage/install"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIs(self.installed_host_network(), True)

    async def test_repair_of_a_git_instance_keeps_it(self):
        env = self.env
        env.stub.dynamic_port_refs.add("main")
        await self.create("lab", channel="git", ref_kind="branch", ref="main", host_network=True)
        shutil.rmtree(os.path.join(env.local_apps, "hri_lab"))
        shutil.rmtree(os.path.join(env.data, "definitions", "lab"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/lab/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on("lab")

    async def test_after_a_killed_update_the_next_start_keeps_and_records_it(self):
        env = self.env
        await self.create()
        env.stub.delay = 0.3
        body = {"version": "0.26.1", "host_network": True}
        with mock.patch.object(type(env.manager), "_rollback_update", new=mock.AsyncMock()):
            _, answer = await env.send("POST", "/api/instances/garage/update", body)
            job = env.manager.jobs.get(answer["job"]["id"])
            for _ in range(300):
                if any(m == "POST" and p.endswith("/update") for m, p, _ in env.stub.calls):
                    break
                await asyncio.sleep(0.01)
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        for _ in range(100):
            if env.stub.installed["local_hri_garage"]["version"] == "0.26.1":
                break
            await asyncio.sleep(0.02)
        env.stub.delay = 0
        await env.manager.startup()
        self.assertEqual((self.config()["host_network"], self.config()["ingress_port"]), (True, 0))
        self.assertIs(env.registry.get("garage")["host_network"], True)
        self.assertIs((await self.row())["host_network"], True)

    # ------------------------------------------------------------------ the installed app differs from the record

    async def lagging_record(self, on: bool = True):
        """The installed app has the host's network (``on``; or has it not), the registry and the definition say the
        other: an update recorded late, an older /data restored, or someone else's change."""
        await self.create(host_network=not on)
        self.env.stub.installed["local_hri_garage"]["definition"]["host_network"] = on
        self.assertIs(self.env.registry.get("garage")["host_network"], not on)

    def manager_made_it(self, on: bool = True):
        entry = self.env.registry.get("garage")
        self.env.registry.update("garage", updating={"at": "t", "fields": {
            **{k: entry.get(k) for k in ("version", "ref_kind", "ref", "sha", "stamp_version", "bluetooth")},
            "host_network": on}})

    def not_followed(self, job, recorded: bool = False):
        env = self.env
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("the manager did not make that change", job["error"])
        self.assertIn("(Host network)", job["error"])
        self.assertIs(env.registry.get("garage")["host_network"], recorded)
        self.assertNotIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)

    async def test_a_lagging_record_is_taken_only_as_the_admin_s_choice(self):
        await self.lagging_record()
        row = await self.row()
        self.assertIs(row["host_network"], True)
        self.assertIn("Host network: the installed app has the host's network, the manager's record says off",
                      row["problem"])
        self.assertIn("the manager did not make that change", row["problem"])
        before = self.config()
        self.not_followed(await self.update(version="0.26.0"))
        self.assertEqual(self.config(), before)
        job = await self.update(version="0.26.0", host_network=True)  # the admin's choice: recorded, restamped
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(job["result"]["restamped"])
        self.assert_on()
        self.assertIn("local_hri_garage", self.env.stub.installed)
        self.assertNotIn("Host network", (await self.row())["problem"] or "")
        job = await self.update(version="0.26.0", host_network=False)  # the other value at the same version: refused
        self.assertEqual(job["state"], "failed")
        self.assertIn("only when the app's version changes", job["error"])

    async def test_a_lagging_record_off_is_taken_as_the_admin_s_choice_too(self):
        """The record says on, the app has it off: an Update at the installed version with Host network off writes
        the definition from HRI's template again, with HRI's own ingress_port."""
        await self.lagging_record(on=False)
        self.not_followed(await self.update(version="0.26.0"), recorded=True)
        job = await self.update(version="0.26.0", host_network=False)
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_off()

    async def test_repair_of_a_lagging_record_needs_the_admin(self):
        env = self.env
        await self.lagging_record()
        shutil.rmtree(os.path.join(env.local_apps, "hri_garage"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.not_followed(job)
        self.assertFalse(os.path.exists(os.path.join(env.local_apps, "hri_garage")))
        self.assertIsInstance(env.registry.get("garage")["needs_attention"], dict)
        self.assertIn("with Host network chosen", env.registry.get("garage")["needs_attention"]["reason"])

    async def repair_follows(self, on: bool):
        env = self.env
        await self.lagging_record(on)
        self.manager_made_it(on)
        shutil.rmtree(os.path.join(env.local_apps, "hri_garage"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(any("Host network: the installed app" in line["msg"] and "the definition follows the app"
                            in line["msg"] for line in job["lines"]))
        # from HRI's template (the copy says the other): off gets HRI's own port back
        (self.assert_on if on else self.assert_off)()

    async def test_repair_follows_a_change_the_manager_made(self):
        await self.repair_follows(True)

    async def test_repair_follows_a_change_to_off_the_manager_made(self):
        await self.repair_follows(False)

    async def test_finish_setup_follows_a_change_the_manager_made(self):
        env = self.env
        await self.lagging_record()
        self.manager_made_it()
        env.registry.update("garage", setup_complete=False)
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_on()

    async def test_finish_setup_of_a_lagging_record_needs_the_admin(self):
        env = self.env
        await self.lagging_record()
        env.registry.update("garage", setup_complete=False)
        env.stub.installed["local_hri_garage"]["state"] = "stopped"
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.not_followed(job)
        self.assertEqual(env.stub.installed["local_hri_garage"]["state"], "stopped")
        self.assertNotIn("host_network", self.config())

    async def test_finish_setup_never_follows_to_off_in_place(self):
        """The record says on, the app has it off (a change the manager made): the stamped definition no longer
        holds HRI's own ingress_port, so it cannot follow in place; an Update at the installed version can."""
        env = self.env
        await self.lagging_record(on=False)
        self.manager_made_it(on=False)
        env.registry.update("garage", setup_complete=False)
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("its definition cannot follow it in place", job["error"])
        self.assertIn("Update it at its installed version with Host network off", job["error"])
        self.assertEqual(self.config()["ingress_port"], 0)
        self.assertIs(env.registry.get("garage")["host_network"], True)

    async def test_only_true_or_false(self):
        env = self.env
        status, body = await env.send("POST", "/api/instances",
                                      {"name": "garage", "version": "0.26.0", "host_network": "yes"})
        self.assertEqual((status, body["error"]), (400, "host_network is true or false."))
        await self.create()
        status, body = await env.send("POST", "/api/instances/garage/update", {"version": "0.26.1", "host_network": 1})
        self.assertEqual((status, body["error"]), (400, "host_network is true or false."))


if __name__ == "__main__":
    unittest.main()
