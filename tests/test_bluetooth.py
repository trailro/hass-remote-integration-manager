"""Bluetooth, per instance: ``host_dbus: true`` in the instance's definition, chosen when it is created (or with an
Update or Rebuild that changes its version), recorded in the manager's registry, and never taken from HRI's template.
The manager's checks (copies.check, and what the Supervisor must report around an install) expect it exactly when the
registry says so."""

import asyncio
import json
import os
import shutil
import unittest
from unittest import mock

import yaml

from hrimgr import children, copies, stamp
from hrimgr.registry import RegistryError

from .env import Env
from .fakes.tarballs import sha_of
from .helpers import FIXTURE_CONFIG, tmpdir


def template() -> dict:
    return stamp.parse_template(FIXTURE_CONFIG.read_bytes())


def stamped(bluetooth: bool, channel: str = "release", version: str = "0.25.0") -> dict:
    return yaml.safe_load(stamp.dump(stamp.stamp(template(), "garage", version, channel, bluetooth), "t"))


class StampTest(unittest.TestCase):
    def test_the_manager_adds_host_dbus_and_nothing_else(self):
        with_bt, without = stamped(True), stamped(False)
        self.assertIs(with_bt.pop("host_dbus"), True)
        self.assertEqual(with_bt, without)
        self.assertNotIn("host_dbus", stamped(False, "git", "0.0.0-0123456789ab"))
        self.assertIs(stamped(True, "git", "0.0.0-0123456789ab")["host_dbus"], True)

    def test_the_template_never_adds_it(self):
        for value in (True, False):
            with self.subTest(value=value), self.assertRaises(stamp.TemplateError):
                stamp.parse_template(yaml.safe_dump({**template(), "host_dbus": value}).encode())

    def test_a_copy_has_it_exactly_when_the_registry_says_bluetooth(self):
        copies.check(stamped(True), "garage", "0.25.0", "release", bluetooth=True)
        copies.check(stamped(False), "garage", "0.25.0", "release", bluetooth=False)
        for config, bluetooth in ((stamped(True), False), (stamped(False), True), ({**stamped(False), "host_dbus": False}, True),
                                  ({**stamped(False), "host_dbus": "yes"}, True)):
            with self.subTest(config=config.get("host_dbus"), bluetooth=bluetooth), self.assertRaises(copies.CopyError):
                copies.check(config, "garage", "0.25.0", "release", bluetooth=bluetooth)

    def test_the_supervisor_must_report_it_exactly_then(self):
        for bluetooth in (True, False):
            expected = stamp.expected_view(stamped(bluetooth), "local_hri_garage")
            self.assertIs(expected["host_dbus"], bluetooth)


class BluetoothFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()

    async def asyncTearDown(self):
        self.env.assert_only_allowed_calls(self)
        await self.env.close()

    def config(self, name="garage"):
        with open(os.path.join(self.env.local_apps, f"hri_{name}", "config.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    async def create(self, name="garage", **body):
        body = {"name": name, "channel": "release", "version": "0.25.0", **body}
        return await self.env.job(await self.env.send("POST", "/api/instances", body))

    async def update(self, name="garage", **body):
        return await self.env.job(await self.env.send("POST", f"/api/instances/{name}/update", body))

    async def row(self, name="garage"):
        _, data = await self.env.get("/api/instances")
        return next(i for i in data["instances"] if i["name"] == name)

    def installed_host_dbus(self, slug="local_hri_garage"):
        return self.env.stub.installed[slug]["definition"]["host_dbus"]

    async def test_created_with_bluetooth(self):
        env = self.env
        job = await self.create(bluetooth=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIs(self.config()["host_dbus"], True)
        self.assertIs(env.registry.get("garage")["bluetooth"], True)
        self.assertIs(self.installed_host_dbus(), True)  # what the check after the install expected
        self.assertIs((await self.row())["bluetooth"], True)
        with open(os.path.join(env.data, "definitions", "garage", "config.yaml"), encoding="utf-8") as fh:
            self.assertIs(yaml.safe_load(fh)["host_dbus"], True)  # the copy in /data holds it too

    async def test_created_without_bluetooth(self):
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        self.assertNotIn("host_dbus", self.config())
        self.assertIs(self.env.registry.get("garage")["bluetooth"], False)
        self.assertIs((await self.row())["bluetooth"], False)

    async def test_the_install_is_checked_against_the_registry_s_choice(self):
        env = self.env
        env.stub.install_override["local_hri_garage"] = {"host_dbus": True}
        job = await self.create()  # without Bluetooth, the Supervisor installed it with D-Bus
        self.assertEqual(job["state"], "failed")
        self.assertIn("host_dbus: True, not False", job["error"])
        self.assertIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)
        env.stub.install_override["local_hri_attic"] = {"host_dbus": False}
        job = await self.create("attic", bluetooth=True)  # with Bluetooth, installed without
        self.assertEqual(job["state"], "failed")
        self.assertIn("host_dbus: False, not True", job["error"])

    async def test_turned_on_with_an_update_to_a_newer_release(self):
        env = self.env
        await self.create()
        job = await self.update(version="0.25.1", bluetooth=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIs(self.config()["host_dbus"], True)
        self.assertIs(env.registry.get("garage")["bluetooth"], True)
        self.assertIs(self.installed_host_dbus(), True)
        job = await self.update()  # no bluetooth in the request: kept as it is
        self.assertIs(env.registry.get("garage")["bluetooth"], True)

    async def test_turned_off_with_an_update(self):
        env = self.env
        await self.create(bluetooth=True)
        job = await self.update(version="0.25.1", bluetooth=False)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertNotIn("host_dbus", self.config())
        self.assertIs(env.registry.get("garage")["bluetooth"], False)
        self.assertIs(self.installed_host_dbus(), False)

    async def test_not_without_a_version_change(self):
        """The Supervisor applies a definition only with a new version: a change at the same version is refused,
        with what to do instead, and nothing is written."""
        env = self.env
        await self.create()
        before = self.config()
        calls = len(env.stub.calls)
        job = await self.update(version="0.25.0", bluetooth=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("only when the app's version changes", job["error"])
        self.assertIn("Delete the instance keeping its data and create it again with Bluetooth on", job["error"])
        self.assertEqual(self.config(), before)
        self.assertIs(env.registry.get("garage")["bluetooth"], False)
        self.assertEqual([c for c in env.stub.calls[calls:] if c[0] == "POST" and c[1] != "/store/reload"], [])

    async def test_a_rebuild_turns_it_on_only_with_a_new_commit(self):
        env = self.env
        job = await self.create("lab", channel="git", ref_kind="branch", ref="main")
        self.assertEqual(job["state"], "succeeded", job)
        job = await self.update("lab", bluetooth=True)  # the branch is still at the installed commit
        self.assertEqual(job["state"], "failed")
        self.assertIn("only when the app's version changes", job["error"])
        env.stub.refs["main"] = sha_of("main-2")  # a new commit: a new version
        job = await self.update("lab", bluetooth=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIs(self.config("lab")["host_dbus"], True)
        self.assertIs(env.registry.get("lab")["bluetooth"], True)

    async def test_repair_and_install_keep_it(self):
        env = self.env
        await self.create(bluetooth=True)
        folder = os.path.join(env.local_apps, "hri_garage")
        for source in ("the copy", "GitHub"):
            with self.subTest(source=source):
                shutil.rmtree(folder)
                if source == "GitHub":
                    shutil.rmtree(os.path.join(env.data, "definitions", "garage"))
                await env.sv.reload_store()
                job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
                self.assertEqual(job["state"], "succeeded", job)
                self.assertIs(self.config()["host_dbus"], True)
                self.assertIs(env.registry.get("garage")["bluetooth"], True)
        del env.stub.installed["local_hri_garage"]  # a definition without its app: Install checks it, with Bluetooth
        job = await env.job(await env.send("POST", "/api/instances/garage/install"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIs(self.installed_host_dbus(), True)

    async def killed_after_the_supervisor_applied(self, body: dict) -> None:
        """An update is killed after the Supervisor had its call, which it then finishes on its own."""
        env = self.env
        env.stub.delay = 0.3
        with mock.patch.object(type(env.manager), "_rollback_update", new=mock.AsyncMock()):
            status, answer = await env.send("POST", "/api/instances/garage/update", body)
            job = env.manager.jobs.get(answer["job"]["id"])
            for _ in range(300):
                if any(m == "POST" and p.endswith("/update") for m, p, _ in env.stub.calls):
                    break
                await asyncio.sleep(0.01)
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        for _ in range(100):  # the Supervisor finishes what it was asked
            if env.stub.installed["local_hri_garage"]["version"] == body["version"]:
                break
            await asyncio.sleep(0.02)
        env.stub.delay = 0

    async def test_after_a_killed_update_the_next_start_keeps_and_records_its_bluetooth(self):
        """An update turning Bluetooth on is killed after the Supervisor applied it: the next start keeps the new
        definition (Bluetooth on) and records it, as the installed app has it."""
        env = self.env
        await self.create()
        await self.killed_after_the_supervisor_applied({"version": "0.25.1", "bluetooth": True})
        await env.manager.startup()  # the next start
        self.assertIs(self.config()["host_dbus"], True)
        self.assertIs(env.registry.get("garage")["bluetooth"], True)
        self.assertIs(self.installed_host_dbus(), True)
        self.assertIs((await self.row())["bluetooth"], True)

    async def lagging_record(self):
        """The state test_after_a_killed_update_... reached before the next start kept the new definition: the registry
        (and the definition) say Bluetooth off while the installed app has the host's D-Bus: an update recorded late,
        or an older /data restored."""
        await self.create()
        self.env.stub.installed["local_hri_garage"]["definition"]["host_dbus"] = True
        self.assertIs(self.env.registry.get("garage")["bluetooth"], False)

    def followed(self):
        """The definition, the record, the marker and the copy say Bluetooth on, and the app was never uninstalled."""
        env = self.env
        self.assertIs(self.config()["host_dbus"], True)
        self.assertIs(env.registry.get("garage")["bluetooth"], True)
        self.assertIsNone(env.registry.get("garage").get("tampered"))
        self.assertIn("local_hri_garage", env.stub.installed)
        self.assertNotIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)
        with open(os.path.join(env.data, "definitions", "garage", "config.yaml"), encoding="utf-8") as fh:
            self.assertIs(yaml.safe_load(fh)["host_dbus"], True)

    def not_followed(self, job):
        """R2-9: the job refused to take the app's host_dbus as the instance's Bluetooth, and wrote nothing of it."""
        env = self.env
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("the manager did not make that change", job["error"])
        self.assertIs(env.registry.get("garage")["bluetooth"], False)
        self.assertNotIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)

    def manager_made_it(self):
        """The registry's flag of an update the manager made, whose record is late, names Bluetooth on."""
        entry = self.env.registry.get("garage")
        self.env.registry.update("garage", updating={"at": "t", "fields": {
            **{k: entry.get(k) for k in ("version", "ref_kind", "ref", "sha", "stamp_version")}, "bluetooth": True}})

    async def test_a_lagging_record_is_taken_only_as_the_admin_s_choice(self):
        """R2-9: the installed app has the host's D-Bus, the record says off, and nothing says the manager made that
        change (the Supervisor's own Update of a definition someone changed would do it): an Update at the same version
        refuses to record it, until the admin chooses Bluetooth on in it."""
        await self.lagging_record()
        row = await self.row()
        self.assertIs(row["bluetooth"], True)
        self.assertIn("Bluetooth: the installed app has the host's D-Bus", row["problem"])
        self.assertIn("the manager did not make that change", row["problem"])
        before = self.config()
        self.not_followed(await self.update(version="0.25.0"))
        self.assertEqual(self.config(), before)
        job = await self.update(version="0.25.0", bluetooth=True)  # the admin's choice: recorded
        self.assertEqual(job["state"], "succeeded", job)
        self.followed()
        self.assertNotIn("Bluetooth", (await self.row())["problem"] or "")
        job = await self.update(version="0.25.0", bluetooth=False)  # the other value at the same version: refused
        self.assertEqual(job["state"], "failed")
        self.assertIn("only when the app's version changes", job["error"])

    async def test_repair_of_a_lagging_record_needs_the_admin(self):
        env = self.env
        await self.lagging_record()
        shutil.rmtree(os.path.join(env.local_apps, "hri_garage"))  # a restore without the local apps folder
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.not_followed(job)
        self.assertFalse(os.path.exists(os.path.join(env.local_apps, "hri_garage")))  # nothing written
        self.assertIsInstance(env.registry.get("garage")["needs_attention"], dict)
        row = await self.row()
        self.assertEqual(row["actions"], ["update", "delete", "repair"])

    async def test_automatic_repair_of_a_lagging_record_needs_the_admin(self):
        env = self.env
        await self.lagging_record()
        shutil.rmtree(os.path.join(env.local_apps, "hri_garage"))
        await env.sv.reload_store()
        env.manager.auto_repair_interval = 300.0
        await env.get("/api/instances")  # the list starts the automatic repair
        await env.manager.jobs.wait_all()
        self.assertIs(env.registry.get("garage")["bluetooth"], False)
        self.assertIsInstance(env.registry.get("garage")["needs_attention"], dict)
        self.assertFalse(os.path.exists(os.path.join(env.local_apps, "hri_garage")))

    async def test_repair_follows_a_change_the_manager_made(self):
        """An update the manager made turned Bluetooth on and was recorded late (its flag names it): that is the
        admin's choice, and Repair records it."""
        env = self.env
        await self.lagging_record()
        self.manager_made_it()
        shutil.rmtree(os.path.join(env.local_apps, "hri_garage"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(any("the definition follows the app" in line["msg"] for line in job["lines"]))
        self.followed()

    async def test_finish_setup_of_a_lagging_record_needs_the_admin(self):
        env = self.env
        await self.lagging_record()
        env.registry.update("garage", setup_complete=False)
        env.stub.installed["local_hri_garage"]["state"] = "stopped"
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.not_followed(job)
        self.assertEqual(env.stub.installed["local_hri_garage"]["state"], "stopped")  # not started
        self.assertNotIn("host_dbus", self.config())
        job = await self.update(version="0.25.0", bluetooth=True)  # the admin's choice
        self.assertEqual(job["state"], "succeeded", job)
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "succeeded", job)
        self.followed()
        with open(os.path.join(env.local_apps, "hri_garage", ".hri-manager.json"), encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["bluetooth"], True)

    async def test_following_the_app_writes_the_registry_first(self):
        """R2-10: when the registry cannot record the Bluetooth the definition follows, the definition is left as it
        was (a definition and a record that disagree refuse every later check), and a later try works."""
        env = self.env
        await self.lagging_record()
        self.manager_made_it()
        env.registry.update("garage", setup_complete=False)
        update = env.registry.update

        def full_disk(name, **fields):
            if "bluetooth" in fields:
                raise RegistryError("the manager's registry cannot be written: No space left on device")
            return update(name, **fields)

        with mock.patch.object(env.registry, "update", side_effect=full_disk):
            job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "failed", job)
        self.assertNotIn("host_dbus", self.config())  # the folder as it was
        with open(os.path.join(env.local_apps, "hri_garage", ".hri-manager.json"), encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["bluetooth"], False)
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "succeeded", job)
        self.followed()

    async def test_only_true_or_false(self):
        env = self.env
        status, body = await env.send("POST", "/api/instances", {"name": "garage", "version": "0.25.0", "bluetooth": "yes"})
        self.assertEqual((status, body["error"]), (400, "bluetooth is true or false."))
        await self.create()
        status, body = await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1", "bluetooth": 1})
        self.assertEqual(status, 400)
        self.assertNotIn("bluetooth", json.dumps(env.stub.calls))


if __name__ == "__main__":
    unittest.main()
