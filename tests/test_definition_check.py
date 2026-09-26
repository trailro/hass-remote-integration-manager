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

    def test_a_sub_folder_swapped_for_a_link_while_it_is_read_is_never_followed(self):
        """R2-16: the walk goes by folder descriptors; a sub-folder swapped for a link to elsewhere after the listing
        is refused or recorded as the link it is, and nothing outside the folder is opened or named."""
        folder = os.path.join(self.root, "hri_garage")
        translations = os.path.join(folder, "translations")
        outside = tmpdir(self)
        with open(os.path.join(outside, "secret"), "wb") as fh:
            fh.write(b"not the manager's")
        real_islink, real_stat, swapped = os.path.islink, os.stat, []

        def swap():
            if not swapped:
                swapped.append(True)
                os.rename(translations, os.path.join(self.root, "moved"))
                os.symlink(outside, translations)

        # right after the walk found translations to be a real folder, by whichever check it makes
        def islink_then_swap(path):
            result = real_islink(path)
            if str(path) == translations and not result:
                swap()
            return result

        def stat_then_swap(path, *args, **kw):
            result = real_stat(path, *args, **kw)
            if kw.get("follow_symlinks") is False and path in ("translations", translations) and not swapped:
                swap()
            return result

        with mock.patch("os.path.islink", side_effect=islink_then_swap), \
                mock.patch("os.stat", side_effect=stat_then_swap):
            try:
                manifest = children.digest_tree(folder)
            except children.UnsafePath:
                manifest = {}
        self.assertTrue(swapped)
        self.assertFalse(any("secret" in k for k in manifest), manifest)
        with self.assertRaises(children.DefinitionChanged):
            children.check_tree(self.root, "garage", self.manifest)

    def test_a_tree_too_deep_is_refused_not_a_crash(self):
        path = os.path.join(self.root, "hri_garage")
        for _ in range(children.MAX_DEPTH + 2):
            path = os.path.join(path, "d")
        os.makedirs(path)
        with self.assertRaises(children.DefinitionChanged):
            children.check_tree(self.root, "garage", self.manifest)


class FlowBase(unittest.IsolatedAsyncioTestCase):
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


class FlowCheckTest(FlowBase):

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

    async def test_a_definition_the_manager_did_not_write_is_flagged_and_repaired(self):
        """At rest, a writer bumps the version and adds a privilege, keeping the marker: the Supervisor's own Update
        button (or auto-update) would install it without the manager. The row says so, the manager refuses everything
        but Repair, Stop and Delete, and Repair writes the manager's definition again."""
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        config = self.config()
        with open(os.path.join(self.folder(), "config.yaml"), "w", encoding="utf-8") as fh:
            yaml.safe_dump({**config, "version": "0.25.9", "privileged": ["SYS_ADMIN"]}, fh)
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        (row,) = data["instances"]
        self.assertIn("the store offers a definition the manager did not write (0.25.9", row["problem"])
        self.assertIn("do not update or rebuild it from the Supervisor's app page", row["problem"])
        self.assertEqual(row["actions"], ["repair", "stop", "delete"])
        for path, body in (("start", {}), ("restart", {}), ("update", {"version": "0.25.1"}), ("finish", {})):
            with self.subTest(action=path):
                status, answer = await env.send("POST", f"/api/instances/garage/{path}", body)
                self.assertEqual(status, 400, answer)
                self.assertIn("did not write", answer["error"])
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        repaired = self.config()
        self.assertEqual(repaired["version"], "0.25.0")
        self.assertNotIn("privileged", repaired)
        _, data = await env.get("/api/instances")
        self.assertNotIn("did not write", data["instances"][0]["problem"] or "")
        self.assertFalse(data["instances"][0]["update_available"])
        status, answer = await env.send("POST", "/api/instances/garage/repair")  # nothing foreign any more
        self.assertEqual(status, 400)

    async def test_a_same_version_definition_the_manager_did_not_write_is_flagged(self):
        """R2-4: at the recorded version, a writer adds what neither Supervisor answer reports (a map of Home
        Assistant's configuration): the Supervisor's own Rebuild would apply it.  The registry records the sha256 of
        the config.yaml the manager wrote; another one is flagged as foreign, and Repair writes the manager's again."""
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        self.assertRegex(env.registry.get("garage")["config_sha256"], r"^[0-9a-f]{64}$")
        with open(os.path.join(self.folder(), "config.yaml"), "a", encoding="utf-8") as fh:
            fh.write("map:\n  - type: homeassistant_config\n    read_only: false\n")
        _, data = await env.get("/api/instances")
        (row,) = data["instances"]
        self.assertIn("hri_garage/config.yaml is not the one the manager wrote for 0.25.0", row["problem"])
        self.assertIn("do not update or rebuild it", row["problem"])
        self.assertEqual(row["actions"], ["repair", "stop", "delete"])
        for path in ("restart", "update", "finish"):
            status, answer = await env.send("POST", f"/api/instances/garage/{path}", {"version": "0.25.1"})
            self.assertEqual(status, 400, (path, answer))
            self.assertIn("is not the one the manager wrote", answer["error"])
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(self.config()["map"], [{"type": "app_config", "read_only": False}])  # HRI's own
        _, data = await env.get("/api/instances")
        self.assertIsNone(data["instances"][0]["problem"])

    async def test_a_mark_set_while_a_request_was_checked_stops_the_job(self):
        """R2-6: a containment can finish between the request's check of the mark and the job: every job body reads
        the mark again."""
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        env.stub.installed["local_hri_garage"]["state"] = "stopped"
        refuse_foreign = env.manager._refuse_foreign

        async def meanwhile_contained(managed):
            await refuse_foreign(managed)
            env.registry.update("garage", tampered={"reason": "contained meanwhile", "uninstalled": False,
                                                    "stopped": False, "failure": "x"})

        for action, body in (("start", {}), ("update", {"version": "0.25.1"})):
            with self.subTest(action=action):
                env.registry.update("garage", tampered=None)
                since = len(env.stub.calls)
                with mock.patch.object(env.manager, "_refuse_foreign", side_effect=meanwhile_contained):
                    job = await env.job(await env.send("POST", f"/api/instances/garage/{action}", body))
                self.assertEqual(job["state"], "failed", job)
                self.assertIn("is marked", job["error"])
                self.assertEqual([c for c in env.stub.calls[since:] if c[0] == "POST" and c[1] != "/store/reload"], [])

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
        status, answer = await env.send("POST", "/api/instances/garage/install")  # not the config.yaml it wrote
        self.assertEqual(status, 400, answer)
        self.assertIn("is not the one the manager wrote", answer["error"])
        # an instance last written by 0.1.2 has no sha256 recorded: the job's own check of the folder refuses it
        env.registry.update("garage", config_sha256=None)
        job = await env.job(await env.send("POST", "/api/instances/garage/install"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("not a definition this manager writes", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install", since))

    async def test_install_refuses_a_file_the_supervisor_would_build_or_confine_it_with(self):
        """R2-2: Dockerfile.<arch>, build.yaml or apparmor.txt next to the config: not what the manager writes."""
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        del env.stub.installed["local_hri_garage"]
        for planted in ("Dockerfile.amd64", "build.yaml", "apparmor.txt"):
            with self.subTest(planted=planted):
                path = os.path.join(self.folder(), planted)
                with open(path, "wb") as fh:
                    fh.write(b"x\n")
                since = len(env.stub.calls)
                job = await env.job(await env.send("POST", "/api/instances/garage/install"))
                os.unlink(path)
                self.assertEqual(job["state"], "failed", job)
                self.assertIn(f"'{planted}'", job["error"])
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



def write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class DecoyScanTest(unittest.TestCase):
    """R2-1: the Supervisor's store reads every config.* of the local apps folder (not in a dot folder or rootfs) and
    keys each by the slug inside it, the last one found winning; the manager searches the folder by the same rule."""

    def setUp(self):
        self.root = tmpdir(self)
        write(os.path.join(self.root, "hri_garage", "config.yaml"), b"slug: hri_garage\nversion: 0.25.0\n")
        write(os.path.join(self.root, "my_app", "config.yaml"), b"slug: my_app\nversion: 1.0.0\n")

    def found(self):
        return {d.path: d.slug for d in stamp.decoys(self.root)}

    def test_an_instance_s_own_definition_is_not_a_decoy(self):
        self.assertEqual(self.found(), {})

    def test_decoys_anywhere_in_the_folder(self):
        cases = {
            "x/config.yaml": b"slug: hri_garage\n",
            "a/b/c/config.yml": b"slug: hri_garage\n",  # nested
            "y/config.json": json.dumps({"slug": "hri_garage"}).encode(),  # another suffix
            "docs/config.example.yaml": b"slug: hri_attic\n",  # config.* with a config suffix is read too
            "hri_attic/config.yaml": b"slug: hri_garage\n",  # an instance folder declaring another instance
            "hri_garage/config.json": b'{"slug": "hri_garage"}',  # beside the instance's own, not it
            "hri_garage/sub/config.yaml": b"slug: hri_garage\n",  # below the instance's own folder
            "z/config.yaml": b"slug: HRI-Garage\n",  # the same host name
        }
        for rel, data in cases.items():
            write(os.path.join(self.root, *rel.split("/")), data)
        found = self.found()
        self.assertEqual(set(found), set(cases))
        self.assertEqual(found["docs/config.example.yaml"], "hri_attic")

    def test_what_the_supervisor_skips_is_skipped(self):
        for rel, data in ((".hidden/config.yaml", b"slug: hri_garage\n"), ("z/rootfs/config.yaml", b"slug: hri_garage\n"),
                          ("z/.git/config.yaml", b"slug: hri_garage\n"), ("w/config.js", b"slug: hri_garage\n"),
                          ("w/config.txt", b"slug: hri_garage\n"), ("v/config.yaml", b"slug: [not yaml\n"),
                          ("u/config.yaml", b"- a list\n"), ("t/notconfig.yaml", b"slug: hri_garage\n")):
            write(os.path.join(self.root, *rel.split("/")), data)
        self.assertEqual(self.found(), {})

    def test_links_folders_are_not_walked_but_a_linked_config_is_read(self):
        outside = tmpdir(self)
        write(os.path.join(outside, "config.yaml"), b"slug: hri_garage\n")
        os.symlink(outside, os.path.join(self.root, "linked"))  # the Supervisor's glob does not follow it either
        self.assertEqual(self.found(), {})
        os.makedirs(os.path.join(self.root, "x"))
        os.symlink(os.path.join(outside, "config.yaml"), os.path.join(self.root, "x", "config.yaml"))
        self.assertEqual(self.found(), {"x/config.yaml": "hri_garage"})

    def test_what_the_manager_cannot_read_is_a_decoy(self):
        os.makedirs(os.path.join(self.root, "f"))
        os.mkfifo(os.path.join(self.root, "f", "config.yaml"))  # could serve the Supervisor any definition
        write(os.path.join(self.root, "big", "config.yaml"), b"slug: hri_garage\n" + b"#" * stamp.MAX_APP_CONFIG)
        self.assertEqual(self.found(), {"f/config.yaml": None, "big/config.yaml": None})

    def test_an_alias_bomb_is_read_without_being_spelled_out(self):
        doc = "a0: &a0 [x, x]\n" + "".join(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]\n" for i in range(1, 30))
        write(os.path.join(self.root, "b", "config.yaml"), (doc + "slug: hri_garage\nname: *a29\n").encode())
        started = time.monotonic()
        (decoy,) = stamp.decoys(self.root)
        self.assertLess(time.monotonic() - started, 2)
        self.assertLess(len(decoy.problem), 400)

    def test_a_folder_too_deep_or_too_large_cannot_be_searched(self):
        path = self.root
        for _ in range(stamp.MAX_SCAN_DEPTH + 1):
            path = os.path.join(path, "d")
        os.makedirs(path)
        with self.assertRaises(stamp.ScanError):
            stamp.decoys(self.root)


class DecoyFlowTest(FlowBase):
    """R2-1 through the manager: a decoy makes create, install, update and start refuse before the Supervisor is
    asked, one that appears around an install is contained, and the page lists every decoy."""

    def decoy(self, rel="x/config.yaml", **over):
        """A copy of the instance's definition elsewhere, with what neither Supervisor answer reports: a map of Home
        Assistant's configuration and another image."""
        config = {**self.config(), "map": [{"type": "homeassistant_config", "read_only": False}],
                  "image": "example.com/evil/img", **over}
        data = json.dumps(config) if rel.endswith(".json") else yaml.safe_dump(config)
        write(os.path.join(self.env.local_apps, *rel.split("/")), data.encode())

    async def listed_decoys(self):
        _, data = await self.env.get("/api/instances")
        return {o["name"]: o for o in data["others"] if o["kind"] == "decoy"}, data

    async def test_create_is_refused_while_a_decoy_appears(self):
        env = self.env
        write_new = children.write_new

        def then_decoy(root, name, build, registry):
            managed = write_new(root, name, build, registry)
            self.decoy()
            return managed

        with mock.patch.object(children, "write_new", then_decoy):
            job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("'x/config.yaml' in the local apps folder declares the slug 'hri_garage'", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install"))
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertFalse(os.path.exists(self.folder()))
        self.assertIsNone(env.registry.get("garage"))
        decoys, _ = await self.listed_decoys()
        self.assertEqual(decoys["x/config.yaml"]["slug"], "local_hri_garage")
        self.assertIn("Remove it", decoys["x/config.yaml"]["problem"])
        # while it is there, no instance is created at all
        status, body = await env.send("POST", "/api/instances", {"name": "attic", "channel": "release", "version": "0.25.0"})
        job = await env.job((status, body))
        self.assertEqual(job["state"], "failed", job)
        self.assertEqual(self.env.changing_calls(), [])

    async def test_what_the_supervisor_skips_does_not_refuse_anything(self):
        env = self.env
        for rel in (".hidden/config.yaml", "z/rootfs/config.yaml", ".hri-old-garage-0123abcd/config.yaml"):
            write(os.path.join(env.local_apps, *rel.split("/")), b"slug: hri_garage\nversion: 9.9.9\n")
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        decoys, _ = await self.listed_decoys()
        self.assertEqual(decoys, {})

    async def test_install_and_start_are_refused_while_a_decoy_is_there(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        env.stub.installed["local_hri_garage"]["state"] = "stopped"
        for rel in ("x/config.yaml", "nested/deeper/config.yml", "j/config.json"):
            with self.subTest(rel=rel):
                self.decoy(rel)
                decoys, data = await self.listed_decoys()
                self.assertIn(rel, decoys)
                (row,) = data["instances"]
                self.assertEqual(row["actions"], ["delete"])
                self.assertIn(f"{rel!r} in the local apps folder declares", row["problem"])
                before = len(env.changing_calls())
                for action in ("start", "restart", "update", "finish"):
                    status, answer = await env.send("POST", f"/api/instances/garage/{action}", {"version": "0.25.1"})
                    self.assertEqual(status, 400, (action, answer))
                    self.assertIn("declares the slug", answer["error"])
                self.assertEqual(env.changing_calls()[before:], [])
                os.unlink(os.path.join(env.local_apps, *rel.split("/")))
        del env.stub.installed["local_hri_garage"]  # a definition restored without its app: Install
        self.decoy("nested/config.yaml")
        status, answer = await env.send("POST", "/api/instances/garage/install")
        self.assertEqual(status, 400, answer)
        # past the request's check, the job's own refuses before the store is reloaded or the app installed
        async def allowed(managed):
            return None

        since = len(env.stub.calls)
        with mock.patch.object(env.manager, "_refuse_foreign", side_effect=allowed):
            job = await env.job(await env.send("POST", "/api/instances/garage/install"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("declares the slug", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/install", since))
        self.assertNotIn(("POST", "/store/reload", {}), env.stub.calls[since:])
        self.assertNotIn("local_hri_garage", env.stub.installed)

    async def test_update_is_refused_while_a_decoy_of_the_new_version_appears(self):
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        replace = children.replace

        def then_decoy(managed, build):
            replacement = replace(managed, build)
            self.decoy(rel="elsewhere/config.yaml")  # a copy of the new definition, with its version
            return replacement

        with mock.patch.object(children, "replace", then_decoy):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("'elsewhere/config.yaml' in the local apps folder declares", job["error"])
        self.assertFalse(self.called("/store/addons/local_hri_garage/update"))
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.25.0")
        self.assertEqual(self.config()["version"], "0.25.0")  # the previous definition is back

    async def test_a_decoy_that_appears_around_the_install_is_contained(self):
        """Between the manager's last look and the Supervisor's install, a reload by anyone (the Supervisor's own
        every 3 hours) can make the store take a decoy: found right after the install, the app is uninstalled."""
        env = self.env
        install = env.sv.install

        async def decoy_then_install(managed):
            self.decoy()
            await env.sv.reload_store()  # someone else's reload
            await install(managed)

        with mock.patch.object(env.sv, "install", side_effect=decoy_then_install):
            job = await self.create()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("after the install or update, 'x/config.yaml' in the local apps folder declares", job["error"])
        self.assertIn("uninstalled at once", job["error"])
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertFalse(self.called("/addons/local_hri_garage/start"))

    async def test_a_folder_changed_around_the_update_is_contained(self):
        """R2-3: the folder is checked right after the update too, before the installed app is compared."""
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        update = env.sv.update

        async def change_then_update(managed):
            with open(os.path.join(self.folder(), "config.yaml"), "ab") as fh:
                fh.write(b"map:\n  - type: homeassistant_config\n")  # neither answer reports it
            await env.sv.reload_store()
            await update(managed)

        with mock.patch.object(env.sv, "update", side_effect=change_then_update):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("was changed after the manager wrote it", job["error"])
        self.assertIn("around the install or update", job["error"])
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertEqual(self.config()["version"], "0.25.0")


if __name__ == "__main__":
    unittest.main()
