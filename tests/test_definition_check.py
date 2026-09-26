"""What the Supervisor installs is the definition the manager wrote.  Anyone who can write the local apps folder can
change ``hri_<name>/config.yaml`` between the manager's write and the Supervisor's reading of it (the store re-reads
the folder at every reload): the manager hashes what it wrote and checks it again before each reload and right before
the install or update, compares the store's parsed definition with what it stamped before the install or update, and
the installed app's after it, uninstalling it at once when they differ."""

import asyncio
import json
import os
import shutil
import time
import unittest
from unittest import mock

import yaml

from hrimgr import children, instances, names, stamp, supervisor

from .env import Env
from .fakes.stub import captured
from .helpers import FIXTURE_CONFIG, tmpdir


def stamped(name: str, version: str, channel: str) -> dict:
    return yaml.safe_load(stamp.dump(stamp.stamp(stamp.parse_template(FIXTURE_CONFIG.read_bytes()), name, version, channel), "t"))


def store_view(fixture: str) -> dict:
    """What SupervisorClient.store_definition makes of a real Supervisor's answer."""
    view = supervisor.pick(captured(fixture)["answer"]["data"], supervisor.STORE_DEFINITION_FIELDS)
    view["version"] = view.pop("version_latest")
    return view


class ViewTest(unittest.TestCase):
    """Against answers captured from a real Supervisor (tests/fixtures/supervisor/) for instances this manager made."""

    def test_a_real_supervisor_reports_what_was_stamped(self):
        release = stamp.expected_view(stamped("garage", "0.25.0", "release"), "local_hri_garage")
        self.assertEqual(stamp.view_differences(store_view("store_app_installed.json"), release, stamp.STORE_VIEW), [])
        info = supervisor.pick(captured("app_info.json")["answer"]["data"], supervisor.APP_DEFINITION_FIELDS)
        self.assertNotIn("options", info)
        self.assertEqual(stamp.view_differences(info, release, stamp.INSTALLED_VIEW), [])
        git = stamp.expected_view(stamped("lab", "0.0.0-cb11571c2cd6", "git"), "local_hri_lab")
        self.assertEqual(stamp.view_differences(store_view("store_app_installed_build.json"), git, stamp.STORE_VIEW), [])

    def test_every_privilege_and_identity_field_is_compared(self):
        expected = stamp.expected_view(stamped("garage", "0.25.0", "release"), "local_hri_garage")
        info = supervisor.pick(captured("app_info.json")["answer"]["data"], supervisor.APP_DEFINITION_FIELDS)
        changes = {"hassio_role": "admin", "hassio_api": True, "homeassistant_api": True, "auth_api": True,
                   "full_access": True, "docker_api": True, "host_network": True, "host_pid": True, "host_ipc": True,
                   "host_uts": True, "host_dbus": True, "privileged": ["SYS_ADMIN"], "devices": ["/dev/mem"],
                   "uart": False, "usb": True, "gpio": True, "video": True, "audio": True, "kernel_modules": True,
                   "devicetree": True, "udev": True, "apparmor": "disable", "version": "0.26.0", "build": True,
                   "ingress": False, "network": {"8087/tcp": None, "22/tcp": 22}, "url": "https://example.com/x",
                   "name": "Something else", "slug": "local_hri_other"}
        self.assertEqual(set(changes), set(stamp.INSTALLED_VIEW))
        for key, value in changes.items():
            with self.subTest(key=key):
                (problem,) = stamp.view_differences({**info, key: value}, expected, stamp.INSTALLED_VIEW)
                self.assertTrue(problem.startswith(f"{key}: "), problem)
        # a port mapped on the Network tab is the user's: the ports' names are what is compared
        self.assertEqual(stamp.view_differences({**info, "network": {"8087/tcp": 8087}}, expected, stamp.INSTALLED_VIEW), [])
        self.assertEqual(stamp.view_differences({**info, "uart": 1}, expected, stamp.INSTALLED_VIEW), ["uart: 1, not True"])
        missing = {k: v for k, v in info.items() if k != "privileged"}
        self.assertEqual(stamp.view_differences(missing, expected, stamp.INSTALLED_VIEW), ["privileged not reported"])

    def test_the_store_view_is_what_the_store_reports(self):
        """api/store.py reports these (extended info); host_dbus, privileged, devices, map, image... it does not."""
        store = captured("store_app_installed.json")["answer"]["data"]
        for key in supervisor.STORE_DEFINITION_FIELDS:
            self.assertIn(key, store)
        for key in ("host_dbus", "host_ipc", "host_uts", "privileged", "devices", "uart", "kernel_modules", "map", "image",
                    "network"):
            self.assertNotIn(key, store)
        self.assertEqual(set(stamp.STORE_VIEW), set(supervisor.STORE_DEFINITION_FIELDS) - {"version_latest"} | {"version"})


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        os.makedirs(os.path.join(self.root, "hri_garage", "translations"))
        for rel, data in (("config.yaml", b"slug: hri_garage\n"), ("translations/en.yaml", b"configuration: {}\n")):
            with open(os.path.join(self.root, "hri_garage", rel), "wb") as fh:
                fh.write(data)
        self.manifest = children.digest_tree(os.path.join(self.root, "hri_garage"))

    def test_unchanged(self):
        self.assertEqual(set(self.manifest), {".", "config.yaml", "translations", "translations/en.yaml"})
        children.check_tree(self.root, "garage", self.manifest)

    def test_changed_and_put_back_as_it_was(self):
        """Same content afterwards: the change time of the file, or of its folder, tells."""
        def config():
            return os.path.join(self.root, "hri_garage", "config.yaml")

        def rewritten():
            with open(config(), "rb") as fh:
                original = fh.read()
            with open(config(), "wb") as fh:
                fh.write(b"slug: hri_garage\nfull_access: true\n")
            with open(config(), "wb") as fh:
                fh.write(original)

        def swapped_by_rename():
            os.rename(config(), config() + ".aside")
            with open(config(), "wb") as fh:
                fh.write(b"slug: hri_garage\nfull_access: true\n")
            os.replace(config() + ".aside", config())  # the original file, its inode, back in place

        for what, change in (("rewritten", rewritten), ("swapped by rename", swapped_by_rename)):
            with self.subTest(what=what):
                self.setUp()
                time.sleep(0.01)
                with open(config(), "rb") as fh:
                    before = fh.read()
                change()
                with open(config(), "rb") as fh:
                    self.assertEqual(fh.read(), before)
                with self.assertRaises(children.DefinitionChanged):
                    children.check_tree(self.root, "garage", self.manifest)

    def test_changed_added_removed_or_linked(self):
        def write(rel, data, mode="wb"):
            with open(os.path.join(self.root, "hri_garage", rel), mode) as fh:
                fh.write(data)

        for what, change in (("changed", lambda: write("config.yaml", b"full_access: true\n", "ab")),
                             ("added", lambda: write("apparmor.txt", b"profile x {}\n")),
                             ("removed", lambda: os.unlink(os.path.join(self.root, "hri_garage", "translations", "en.yaml"))),
                             ("linked", lambda: os.symlink("/", os.path.join(self.root, "hri_garage", "translations", "root")))):
            with self.subTest(what=what):
                self.setUp()
                change()
                with self.assertRaises(children.DefinitionChanged):
                    children.check_tree(self.root, "garage", self.manifest)


class FlowCheckTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()

    async def asyncTearDown(self):
        self.env.assert_only_allowed_calls(self)
        await self.env.close()

    def folder(self, name="garage"):
        return os.path.join(self.env.local_apps, f"hri_{name}")

    def config(self, name="garage"):
        with open(os.path.join(self.folder(name), "config.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    async def create(self, name="garage", **body):
        body = {"name": name, "channel": "release", "version": "0.25.0", **body}
        return await self.env.job(await self.env.send("POST", "/api/instances", body))

    def called(self, path: str, since: int = 0) -> bool:
        return any(m == "POST" and p == path for m, p, _ in self.env.stub.calls[since:])

    async def test_a_store_definition_with_another_role_is_never_installed(self):
        env = self.env
        env.stub.store_override["local_hri_garage"] = {"hassio_role": "admin"}
        job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("hassio_role: 'admin', not 'default'", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install"))
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertEqual(os.listdir(env.local_apps), [])
        self.assertIsNone(env.registry.get("garage"))

    async def test_every_reported_privilege_refuses_the_install(self):
        env = self.env
        for key, value in (("full_access", True), ("docker_api", True), ("host_network", True), ("host_pid", True),
                           ("hassio_api", True), ("auth_api", True), ("homeassistant_api", True), ("apparmor", "disable")):
            with self.subTest(key=key):
                env.stub.store_override["local_hri_garage"] = {key: value}
                job = await self.create()
                self.assertEqual(job["state"], "failed", job)
                self.assertIn(f"{key}: ", job["error"])
                self.assertFalse(self.called("/store/addons/local_hri_garage/install"))

    async def test_an_update_whose_store_definition_changed_is_refused_before_the_update(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        env.stub.store_override["local_hri_garage"] = {"hassio_role": "admin"}
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("hassio_role", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/update"))
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.25.0")
        self.assertEqual(self.config()["version"], "0.25.0")  # the previous definition is back
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])

    async def test_an_install_of_another_definition_is_uninstalled_at_once(self):
        env = self.env
        env.stub.install_override["local_hri_garage"] = {"privileged": ["SYS_ADMIN"]}
        job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("privileged: ['SYS_ADMIN'], not []", job["error"])
        self.assertIn("uninstalled at once", job["error"])
        self.assertIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)
        self.assertFalse(self.called("/addons/local_hri_garage/start"))
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertEqual(os.listdir(env.local_apps), [])
        entry = env.registry.get("garage")  # kept, marked: the row says what happened
        self.assertIn("SYS_ADMIN", entry["tampered"]["reason"])
        _, data = await env.get("/api/instances")
        (orphan,) = [o for o in data["others"] if o.get("instance") == "garage"]
        self.assertIn("it was stopped and uninstalled at once", orphan["problem"])
        self.assertEqual(orphan["actions"], ["forget"])

    async def test_an_update_to_another_definition_is_uninstalled_and_the_previous_one_put_back(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        env.stub.install_override["local_hri_garage"] = {"docker_api": True}
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("docker_api: True, not False", job["error"])
        self.assertIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertEqual(self.config()["version"], "0.25.0")
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertIn("docker_api", env.registry.get("garage")["tampered"]["reason"])
        _, data = await env.get("/api/instances")
        (inst,) = data["instances"]
        self.assertEqual(inst["actions"], ["delete"])  # marked: nothing that installs it again
        self.assertIn("it was stopped and uninstalled at once", inst["problem"])
        env.stub.install_override.clear()
        status, body = await env.send("POST", "/api/instances/garage/install")
        self.assertEqual(status, 400)
        self.assertIn("is marked", body["error"])
        job = await env.job(await env.send("DELETE", "/api/instances/garage", {"remove_data": False, "confirm": "garage"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIsNone(env.registry.get("garage"))  # the mark goes with Delete; the name can be created again
        self.assertEqual((await self.create())["state"], "succeeded")

    def break_marker_after_the_install_check(self):
        """A writer breaks the marker right after the manager read the installed app: the allow-list then refuses
        the stop and the uninstall, which need it."""
        env = self.env
        read = env.sv.app_definition

        async def then_break(slug):
            view = await read(slug)
            path = os.path.join(self.folder(), children.MARKER)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({**data, "instance_id": "f" * 32}, fh)
            return view

        return mock.patch.object(env.sv, "app_definition", side_effect=then_break)

    async def test_a_create_whose_uninstall_is_blocked_says_so_and_stays_marked(self):
        env = self.env
        env.stub.install_override["local_hri_garage"] = {"hassio_role": "admin"}
        with self.break_marker_after_the_install_check():
            job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("NOT", job["error"])
        self.assertNotIn("uninstalled at once", job["error"])
        self.assertIn("stop and uninstall local_hri_garage yourself in Settings > Apps now", job["error"])
        self.assertIn("local_hri_garage", env.stub.installed)  # the Supervisor still has it
        mark = env.registry.get("garage")["tampered"]
        self.assertEqual((mark["uninstalled"], "allow-list" in mark["failure"]), (False, True))
        _, data = await env.get("/api/instances")
        (row,) = [o for o in data["others"] if o["slug"] == "local_hri_garage" and o["kind"] == "folder"]
        self.assertIn("it was NOT", row["problem"])
        self.assertIn("Forget it here", row["problem"])

    async def test_an_update_whose_stop_is_blocked_says_so_and_refuses_everything_else(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        env.stub.install_override["local_hri_garage"] = {"full_access": True}
        with self.break_marker_after_the_install_check():
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("It was NOT stopped nor uninstalled", job["error"])
        self.assertEqual(env.stub.installed["local_hri_garage"]["state"], "started")
        self.assertFalse(env.registry.get("garage")["tampered"]["stopped"])
        # the definition put back has the manager's marker again: the actions show, and all but stop and delete refuse
        self.assertEqual(self.config()["version"], "0.25.0")
        _, data = await env.get("/api/instances")
        (inst,) = data["instances"]
        self.assertEqual(inst["actions"], ["stop", "delete"])
        for path, body in (("start", {}), ("restart", {}), ("update", {"version": "0.25.1"}), ("finish", {}),
                           ("install", {}), ("repair", {})):
            with self.subTest(action=path):
                status, answer = await env.send("POST", f"/api/instances/garage/{path}", body)
                self.assertEqual(status, 400, answer)
                self.assertIn("is marked", answer["error"])
        job = await env.job(await env.send("POST", "/api/instances/garage/stop"))
        self.assertEqual(job["state"], "succeeded", job)

    async def test_a_containment_cut_short_by_a_stop_says_so_and_the_next_start_finishes_it(self):
        env = self.env
        env.stub.install_override["local_hri_garage"] = {"hassio_role": "admin"}
        never = asyncio.Event()

        async def hangs(managed, remove_config):
            await never.wait()

        with mock.patch.object(instances, "CONTAIN_BOUND", 0.2), mock.patch.object(env.sv, "uninstall", side_effect=hangs):
            status, body = await env.send("POST", "/api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"})
            job = env.manager.jobs.get(body["job"]["id"])
            for _ in range(300):
                if any("uninstalling it at once" in line["msg"] for line in job.lines):
                    break
                await asyncio.sleep(0.01)
            job.task.cancel()  # the manager is stopped
            await asyncio.gather(job.task, return_exceptions=True)
        mark = env.registry.get("garage")["tampered"]
        self.assertEqual((mark["uninstalled"], mark["failure"]), (False, instances.INTERRUPTED))
        _, data = await env.get("/api/instances")
        problem = data["instances"][0]["problem"]
        self.assertIn("interrupted: the manager stopped", problem)
        self.assertNotIn("None", problem)
        notes = await env.manager.startup()  # the next start contains it again
        self.assertIn("containing it again", " ".join(notes))
        await env.manager.jobs.wait_all()
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertTrue(env.registry.get("garage")["tampered"]["uninstalled"])

    def installed_as(self, **fields):
        """What the Supervisor holds of the installed app, changed behind the manager's back."""
        self.env.stub.installed["local_hri_garage"]["definition"].update(fields)

    async def test_finish_setup_checks_the_installed_app_first(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        env.registry.update("garage", setup_complete=False)
        env.stub.installed["local_hri_garage"]["state"] = "stopped"
        self.installed_as(hassio_role="admin")
        since = len(env.stub.calls)
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("hassio_role: 'admin', not 'default'", job["error"])
        self.assertFalse(self.called("/addons/local_hri_garage/start", since))
        self.assertFalse(self.called("/addons/local_hri_garage/options", since))
        self.assertNotIn("local_hri_garage", env.stub.installed)

    async def test_an_update_the_supervisor_already_made_is_checked_before_it_is_recorded(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        # the Supervisor finished an update the manager had stopped waiting for, from a changed definition
        env.stub.installed["local_hri_garage"]["version"] = "0.25.1"
        self.installed_as(docker_api=True)
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("docker_api: True, not False", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/update"))
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertEqual(env.registry.get("garage")["version"], "0.25.0")  # never recorded

    async def test_repair_checks_the_installed_app_before_it_takes_it_over(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        shutil.rmtree(self.folder())
        await env.sv.reload_store()
        self.installed_as(full_access=True)
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("full_access: True, not False", job["error"])
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertTrue(env.registry.get("garage")["tampered"]["uninstalled"])

    def unreported(self, key):
        """A Supervisor whose app info no longer carries ``key`` (its API changed)."""
        env = self.env
        read = env.sv.app_definition

        async def without(slug):
            return {k: v for k, v in (await read(slug)).items() if k != key}

        return mock.patch.object(env.sv, "app_definition", side_effect=without)

    async def test_a_field_the_supervisor_stops_reporting_stops_and_marks_without_uninstalling(self):
        env = self.env
        with self.unreported("privileged"):
            job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("does not report privileged", job["error"])
        self.assertIn("a change of the Supervisor's API?", job["error"])
        self.assertIn("kept installed, with its options and data", job["error"])
        self.assertNotIn("uninstall", [p.rsplit("/", 1)[-1] for m, p, _ in env.stub.calls if m == "POST"])
        self.assertIn("local_hri_garage", env.stub.installed)
        self.assertTrue(os.path.isdir(self.folder()))
        mark = env.registry.get("garage")["tampered"]
        self.assertEqual((mark["unverified"], mark["uninstalled"]), (True, False))
        _, data = await env.get("/api/instances")
        (row,) = data["instances"]
        self.assertEqual((row["actions"], row["labels"]["finish"]), (["finish", "delete"], "Check again"))
        for action in ("start", "update", "install"):  # nothing else while it is not checked
            status, _ = await env.send("POST", f"/api/instances/garage/{action}", {"version": "0.25.1"})
            self.assertEqual(status, 400, action)
        # the Supervisor reports the field again (the manager was updated, say): Check again clears the mark
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIsNone(env.registry.get("garage")["tampered"])
        self.assertEqual(env.stub.installed["local_hri_garage"]["state"], "started")

    async def test_repair_of_an_unchecked_instance_checks_it_again(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        with self.unreported("privileged"):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertIn("does not report privileged", job["error"])
        shutil.rmtree(self.folder())  # restored without the local apps folder
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"][0]["actions"], ["repair", "delete"])
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIsNone(env.registry.get("garage")["tampered"])
        with self.unreported("privileged"):  # still not reported: marked again, not uninstalled
            shutil.rmtree(self.folder())
            await env.sv.reload_store()
            env.registry.update("garage", tampered={"reason": "x", "unverified": True, "stopped": True})
            job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed")
        self.assertTrue(env.registry.get("garage")["tampered"]["unverified"])
        self.assertIn("local_hri_garage", env.stub.installed)

    async def test_an_update_the_supervisor_cannot_report_is_recorded_stopped_and_marked(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        with self.unreported("host_dbus"):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("does not report host_dbus", job["error"])
        app = env.stub.installed["local_hri_garage"]
        self.assertEqual((app["version"], app["state"]), ("0.25.1", "stopped"))  # updated, stopped, not uninstalled
        entry = env.registry.get("garage")
        self.assertEqual((entry["version"], entry["updating"], entry["tampered"]["unverified"]), ("0.25.1", None, True))
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertEqual(self.config()["version"], "0.25.1")

    async def test_a_definition_changed_before_the_store_reads_it_is_refused(self):
        env = self.env
        write_new = children.write_new

        def then_change(root, name, build, registry):
            managed = write_new(root, name, build, registry)
            with open(os.path.join(root, f"hri_{name}", "config.yaml"), "ab") as fh:
                fh.write(b"privileged:\n  - SYS_ADMIN\n")  # the store does not report privileged
            return managed

        with mock.patch.object(children, "write_new", then_change):
            job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("hri_garage was changed after the manager wrote it ('config.yaml')", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install"))
        self.assertEqual(os.listdir(env.local_apps), [])

    async def test_a_definition_changed_after_the_store_read_it_is_refused(self):
        env = self.env
        scan = env.stub._scan

        def scan_then_change():
            found = scan()
            config = os.path.join(self.folder(), "config.yaml")
            if "local_hri_garage" in found and os.path.exists(config):
                with open(config, "ab") as fh:
                    fh.write(b"devices:\n  - /dev/mem\n")
            return found

        env.stub._scan = scan_then_change
        job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("changed after the manager wrote it", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install"))

    async def test_a_definition_swapped_while_the_store_reads_it_and_put_back_is_refused(self):
        """The writer puts a changed config.yaml in place just for the Supervisor's reload (privileged: neither
        Supervisor answer reports it) and puts the original back: content identical, but it was written."""
        env = self.env
        scan = env.stub._scan

        def swap_scan_restore():
            config = os.path.join(self.folder(), "config.yaml")
            if not os.path.exists(config):
                return scan()
            with open(config, "rb") as fh:
                original = fh.read()
            with open(config, "ab") as fh:
                fh.write(b"privileged:\n  - SYS_ADMIN\n")
            try:
                return scan()
            finally:
                with open(config, "wb") as fh:
                    fh.write(original)

        env.stub._scan = swap_scan_restore
        job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("hri_garage was changed after the manager wrote it", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install"))

    async def test_install_of_a_definition_it_did_not_just_write_checks_it_first(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        del env.stub.installed["local_hri_garage"]  # a definition restored without its app
        with open(os.path.join(self.folder(), "config.yaml"), "ab") as fh:
            fh.write(b"full_access: true\n")
        since = len(env.stub.calls)
        job = await env.job(await env.send("POST", "/api/instances/garage/install"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("not a definition this manager writes", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install", since))

    async def test_the_marker_and_registry_are_what_they_were(self):
        """The checks read only; a normal create is unchanged and its log says what was checked."""
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        lines = [l["msg"] for l in job["lines"]]
        self.assertIn("the store's definition is the one the manager wrote", lines)
        self.assertIn("the installed definition is the one the manager wrote", lines)
        with open(os.path.join(self.folder(), children.MARKER), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["slug"], names.supervisor_slug("garage"))


if __name__ == "__main__":
    unittest.main()
