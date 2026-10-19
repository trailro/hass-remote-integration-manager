"""Partial restores, since Supervisor 2026.10.0 put the local apps folder (backup folder addons/local) back into full
backups: a restore of only that folder brings back an instance folder the manager deleted since, or an older
definition of an instance it updated since; a restore of only the manager brings back a registry from before an
instance was created or updated.  The manager adopts a folder that holds exactly what it writes, removes one it cannot
adopt on request, writes a rolled-back definition again from its copy by itself, and words what it cannot tell
neutrally."""

import asyncio
import json
import os
import shutil
import unittest
import unittest.mock

import yaml

from hrimgr import children, instances, jobs, registry

from .env import Env
from .helpers import tmpdir


class RestoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()
        self.saved = tmpdir(self)

    async def asyncTearDown(self):
        self.env.assert_only_allowed_calls(self)
        await self.env.close()

    def folder(self, name):
        return os.path.join(self.env.local_apps, f"hri_{name}")

    def copy_dir(self, name):
        return os.path.join(self.env.data, "definitions", name)

    def marker(self, name):
        with open(os.path.join(self.folder(name), children.MARKER), encoding="utf-8") as fh:
            return json.load(fh)

    def config(self, name):
        with open(os.path.join(self.folder(name), "config.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    async def create(self, name, **body):
        body = {"name": name, "channel": "release", "version": "0.25.0", **body}
        job = await self.env.job(await self.env.send("POST", "/api/instances", body))
        self.assertEqual(job["state"], "succeeded", job)

    def back_up(self, what):
        """A backup of the local apps folder ("local") or of the manager's /data ("data")."""
        source = self.env.local_apps if what == "local" else self.env.data
        shutil.copytree(source, os.path.join(self.saved, what), symlinks=True)

    async def restore(self, what):
        """A partial restore of that one backup folder, as the Supervisor does it: replaced whole."""
        target = self.env.local_apps if what == "local" else self.env.data
        shutil.rmtree(target)
        shutil.copytree(os.path.join(self.saved, what), target, symlinks=True)
        await self.env.sv.reload_store()

    async def lists(self):
        _, data = await self.env.get("/api/instances")
        return {i["name"]: i for i in data["instances"]}, {o["slug"]: o for o in data["others"]}

    async def wait_jobs(self):
        for _ in range(500):
            if not any(j.state == "running" for j in self.env.manager.jobs.recent()):
                return
            await asyncio.sleep(0.02)
        raise AssertionError("jobs still running")

    # ------------------------------------------------------------------ adopt (C and D1)

    async def test_a_deleted_instance_s_folder_restored_alone_is_adopted(self):
        """C: deleted through the manager, then a restore of only the local apps folder brings its folder back.  It
        holds exactly what the manager writes: Adopt (name typed) records it again from its marker, keeps a copy, and
        Install works from there."""
        env = self.env
        await self.create("attic")
        instance_id = self.marker("attic")["instance_id"]
        self.back_up("local")
        job = await env.job(await env.send("DELETE", "/api/instances/attic", {"remove_data": False, "confirm": "attic"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.restore("local")

        rows, others = await self.lists()
        self.assertNotIn("attic", rows)
        other = others["local_hri_attic"]
        self.assertEqual((other["kind"], other["actions"], other["instance"]), ("folder", ["adopt", "remove_folder"], "attic"))
        self.assertTrue(other["adoptable"])
        self.assertIn("registry has no instance attic", other["problem"])
        self.assertIn("probably a folder restored from a backup", other["problem"])
        self.assertIn("Adopt makes it this manager's instance again", other["problem"])

        status, answer = await env.send("POST", "/api/instances/attic/adopt", {})
        self.assertEqual(status, 400, answer)
        self.assertIn("needs its name typed", answer["error"])
        before = os.listdir(self.folder("attic"))
        env.stub.calls.clear()
        job = await env.job(await env.send("POST", "/api/instances/attic/adopt", {"confirm": "attic"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.changing_calls(), [])  # nothing installed, nothing reloaded
        self.assertEqual(sorted(os.listdir(self.folder("attic"))), sorted(before))
        entry = env.registry.get("attic")
        self.assertEqual((entry["instance_id"], entry["slug"], entry["channel"], entry["version"]),
                         (instance_id, "local_hri_attic", "release", "0.25.0"))
        self.assertFalse(entry["setup_complete"])
        self.assertRegex(entry["config_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(entry["history"][-1]["event"].startswith("adopted at "), entry["history"])
        self.assertTrue(entry["history"][-1]["event"].endswith(", not installed"))
        self.assertEqual(entry["history"][-1]["by"], "alice")
        self.assertTrue(os.path.isfile(os.path.join(self.copy_dir("attic"), "config.yaml")))

        rows, others = await self.lists()
        self.assertNotIn("local_hri_attic", others)
        self.assertTrue(rows["attic"]["managed"])
        self.assertEqual(rows["attic"]["actions"], ["install", "delete"])
        job = await env.job(await env.send("POST", "/api/instances/attic/install"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.installed["local_hri_attic"]["state"], "started")
        status, answer = await env.send("POST", "/api/instances/attic/adopt", {"confirm": "attic"})
        self.assertEqual(status, 400, answer)  # in the registry now
        self.assertIn("is in the manager's registry", answer["error"])

    async def test_an_instance_created_after_the_manager_s_backup_is_adopted_installed(self):
        """D1: a restore of only the manager brings back a registry from before the instance was created; it is
        installed and running.  Adopt records it with the installed version, its setup complete."""
        env = self.env
        self.back_up("data")
        await self.create("attic")
        await self.restore("data")
        _, others = await self.lists()
        other = others["local_hri_attic"]
        self.assertEqual(other["actions"], ["adopt"])
        self.assertIn("records the installed local_hri_attic 0.25.0", other["problem"])
        job = await env.job(await env.send("POST", "/api/instances/attic/adopt", {"confirm": "attic"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["result"], {"adopted": "local_hri_attic", "version": "0.25.0", "installed": True})
        entry = env.registry.get("attic")
        self.assertTrue(entry["setup_complete"])
        self.assertTrue(entry["history"][-1]["event"].endswith(", installed"))
        rows, others = await self.lists()
        self.assertEqual(rows["attic"]["actions"], ["restart", "stop", "update", "delete"])
        self.assertIsNone(rows["attic"]["problem"])
        job = await env.job(await env.send("POST", "/api/instances/attic/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)

    async def adopt_git(self):
        env = self.env
        self.back_up("data")
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        await self.restore("data")
        _, others = await self.lists()
        self.assertEqual(others["local_hri_lab"]["actions"], ["adopt"])
        self.assertIn("downloaded to compare the whole tree", others["local_hri_lab"]["problem"])
        env.stub.codeload_paths.clear()
        return self.marker("lab")["sha"]

    async def test_a_git_instance_is_adopted_after_its_commit_is_compared(self):
        """A git instance: the recorded commit is downloaded and the whole tree compared with what the manager builds
        from it."""
        env = self.env
        sha = await self.adopt_git()
        job = await env.job(await env.send("POST", "/api/instances/lab/adopt", {"confirm": "lab"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.codeload_paths, [sha])  # the recorded commit, never the branch's head
        entry = env.registry.get("lab")
        self.assertEqual((entry["channel"], entry["sha"], entry["ref"]), ("git", sha, "main"))
        self.assertEqual(sorted(os.listdir(self.copy_dir("lab"))), ["config.yaml", "copy.json"])
        rows, _ = await self.lists()
        self.assertIsNone(rows["lab"]["problem"])

    async def test_a_git_tree_changed_in_any_file_is_not_adopted(self):
        env = self.env
        sha = await self.adopt_git()
        with open(os.path.join(self.folder("lab"), "Dockerfile"), "ab") as fh:
            fh.write(b"RUN echo changed\n")
        job = await env.job(await env.send("POST", "/api/instances/lab/adopt", {"confirm": "lab"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn(f"is not what the manager builds from commit {sha[:12]} ('Dockerfile')", job["error"])
        self.assertIsNone(env.registry.get("lab"))

    async def test_an_installed_app_that_differs_from_its_folder_is_not_adopted(self):
        env = self.env
        self.back_up("data")
        await self.create("attic")
        await self.restore("data")
        env.stub.installed["local_hri_attic"]["definition"]["privileged"] = ["SYS_ADMIN"]
        job = await env.job(await env.send("POST", "/api/instances/attic/adopt", {"confirm": "attic"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("is not what hri_attic/ defines (privileged", job["error"])
        self.assertIsNone(env.registry.get("attic"))
        self.assertFalse(os.path.exists(self.copy_dir("attic")))

    # ------------------------------------------------------------------ cannot be adopted: Remove folder

    async def test_a_restored_folder_that_does_not_verify_can_only_be_removed(self):
        """The folder's definition is not what the manager writes: no Adopt (refused by the job too); Remove folder
        (name typed) removes that folder only, refused while its app is installed."""
        env = self.env
        await self.create("attic")
        await self.create("garage")
        self.back_up("local")
        job = await env.job(await env.send("DELETE", "/api/instances/attic", {"remove_data": False, "confirm": "attic"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.restore("local")
        path = os.path.join(self.folder("attic"), "config.yaml")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("privileged:\n  - SYS_ADMIN\n")
        _, others = await self.lists()
        other = others["local_hri_attic"]
        self.assertEqual(other["actions"], ["remove_folder"])
        self.assertIn("It cannot be adopted", other["problem"])
        self.assertIn("Remove folder deletes hri_attic/ and nothing else", other["problem"])
        job = await env.job(await env.send("POST", "/api/instances/attic/adopt", {"confirm": "attic"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("is not a definition this manager writes", job["error"])
        self.assertIsNone(env.registry.get("attic"))

        status, answer = await env.send("POST", "/api/instances/attic/remove_folder", {})
        self.assertEqual(status, 400, answer)
        status, answer = await env.send("POST", "/api/instances/garage/remove_folder", {"confirm": "garage"})
        self.assertEqual(status, 400, answer)  # an instance of the registry: Delete's
        self.assertIn("is in the manager's registry", answer["error"])
        job = await env.job(await env.send("POST", "/api/instances/attic/remove_folder", {"confirm": "attic"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertFalse(os.path.lexists(self.folder("attic")))
        self.assertTrue(os.path.isdir(self.folder("garage")))
        self.assertNotIn("local_hri_attic", env.stub.store)
        _, others = await self.lists()
        self.assertNotIn("local_hri_attic", others)

    async def test_an_adoptable_folder_is_removed_only_when_that_is_confirmed(self):
        """F8: Remove folder of a folder that could be adopted needs adoptable: true as well (the page sends it from the
        row of such a folder, saying so)."""
        env = self.env
        await self.create("attic")
        self.back_up("local")
        job = await env.job(await env.send("DELETE", "/api/instances/attic", {"remove_data": False, "confirm": "attic"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.restore("local")
        job = await env.job(await env.send("POST", "/api/instances/attic/remove_folder", {"confirm": "attic"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("holds what the manager writes: Adopt it", job["error"])
        self.assertTrue(os.path.isdir(self.folder("attic")))
        job = await env.job(await env.send("POST", "/api/instances/attic/remove_folder",
                                           {"confirm": "attic", "adoptable": True}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertFalse(os.path.lexists(self.folder("attic")))

    async def test_no_adoption_of_an_installed_app_while_a_decoy_names_it(self):
        env = self.env
        self.back_up("data")
        await self.create("attic")
        await self.restore("data")
        os.makedirs(os.path.join(env.local_apps, "x"))
        self._append(os.path.join(env.local_apps, "x", "config.yaml"), "slug: hri_attic\nversion: '9'\n")
        job = await env.job(await env.send("POST", "/api/instances/attic/adopt", {"confirm": "attic"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("so it is not adopted", job["error"])
        self.assertIsNone(env.registry.get("attic"))

    async def test_an_installed_app_whose_folder_does_not_verify_gets_no_action(self):
        env = self.env
        self.back_up("data")
        await self.create("attic")
        await self.restore("data")
        with open(os.path.join(self.folder("attic"), "config.yaml"), "a", encoding="utf-8") as fh:
            fh.write("privileged:\n  - SYS_ADMIN\n")
        _, others = await self.lists()
        other = others["local_hri_attic"]
        self.assertEqual(other["actions"], [])
        self.assertIn("uninstall local_hri_attic in Settings > Apps", other["problem"])
        job = await env.job(await env.send("POST", "/api/instances/attic/remove_folder", {"confirm": "attic"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("local_hri_attic is installed", job["error"])
        self.assertTrue(os.path.isdir(self.folder("attic")))

    # ------------------------------------------------------------------ Record (D2, F3) and the registry's history

    async def test_a_registry_behind_the_folder_and_the_app_is_recorded(self):
        """D2: updated after the manager's backup, then a restore of only the manager: the folder holds the manager's own
        definition of the installed version, the record an older one.  Said in neutral words, never acted on by
        itself; Record (name typed) makes the record follow, nothing downloaded or installed."""
        env = self.env
        await self.create("garage")
        self.back_up("data")
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.restore("data")
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.manager.rolled_back_settle = 0
        for _ in range(2):
            rows, _ = await self.lists()
        row = rows["garage"]
        self.assertIsNone(row["job"])
        self.assertIn("hri_garage/ holds the manager's definition of 0.25.1, the installed version, but the manager's "
                      "record says 0.25.0", row["problem"])
        self.assertNotIn("omeone changed", row["problem"])
        self.assertEqual(row["actions"], ["record", "stop", "delete"])
        status, answer = await env.send("POST", "/api/instances/garage/restart")
        self.assertEqual(status, 400)
        self.assertIn("Record makes the manager's record follow them", answer["error"])
        status, answer = await env.send("POST", "/api/instances/garage/record", {})
        self.assertEqual(status, 400, answer)
        env.stub.calls.clear()
        env.stub.codeload_paths.clear()
        job = await env.job(await env.send("POST", "/api/instances/garage/record", {"confirm": "garage"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual((env.changing_calls(), env.stub.codeload_paths), ([], []))
        entry = env.registry.get("garage")
        self.assertEqual(entry["version"], "0.25.1")
        self.assertEqual(entry["config_sha256"], env.manager._config_sha256("garage"))
        self.assertEqual(entry["history"][-1]["version"], "0.25.0")
        self.assertIn("the record was behind the folder and the installed app (0.25.0 -> 0.25.1)",
                      entry["history"][-1]["event"])
        with open(os.path.join(self.copy_dir("garage"), "copy.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], "0.25.1")
        rows, _ = await self.lists()
        self.assertIsNone(rows["garage"]["problem"])
        status, answer = await env.send("POST", "/api/instances/garage/record", {"confirm": "garage"})
        self.assertEqual(status, 400, answer)  # nothing to record any more

    async def test_repair_says_when_the_registry_was_behind(self):
        """The same state, Repaired through the API instead: the history says the record was behind."""
        env = self.env
        await self.create("garage")
        self.back_up("data")
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.restore("data")
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        last = env.registry.get("garage")["history"][-1]
        self.assertEqual(last["version"], "0.25.0")
        self.assertIn("the manager's record was behind the installed app (0.25.0 -> 0.25.1)", last["event"])

    async def test_a_folder_and_app_restored_from_an_older_backup_are_recorded_not_repaired(self):
        """F3: a restore of a folder and its app from before an update: the folder holds the manager's definition of
        the installed (older) version, the record the newer.  Never repaired by itself; Record, no download."""
        env = self.env
        await self.roll_back()
        env.stub.installed["local_hri_garage"]["version"] = "0.25.0"  # then the app's restore
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.manager.rolled_back_settle = 0
        for _ in range(2):
            rows, _ = await self.lists()
        self.assertIsNone(rows["garage"]["job"])
        self.assertEqual(rows["garage"]["actions"], ["record", "stop", "delete"])
        self.assertIn("after restoring a folder and its app from an older backup", rows["garage"]["problem"])
        env.stub.codeload_paths.clear()
        job = await env.job(await env.send("POST", "/api/instances/garage/record", {"confirm": "garage"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.codeload_paths, [])
        entry = env.registry.get("garage")
        self.assertEqual(entry["version"], "0.25.0")
        self.assertIn("the record was ahead of the folder and the installed app (0.25.1 -> 0.25.0)",
                      entry["history"][-1]["event"])
        rows, _ = await self.lists()
        self.assertIsNone(rows["garage"]["problem"])

    async def test_record_refuses_an_installed_app_that_differs(self):
        env = self.env
        await self.roll_back()
        env.stub.installed["local_hri_garage"]["version"] = "0.25.0"
        env.stub.installed["local_hri_garage"]["definition"]["privileged"] = ["SYS_ADMIN"]
        job = await env.job(await env.send("POST", "/api/instances/garage/record", {"confirm": "garage"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("is not what hri_garage/ defines (privileged", job["error"])
        self.assertEqual(env.registry.get("garage")["version"], "0.25.1")

    async def test_a_definition_the_manager_would_never_write_keeps_the_strong_words(self):
        env = self.env
        await self.create("garage")
        config = self.config("garage")
        with open(os.path.join(self.folder("garage"), "config.yaml"), "w", encoding="utf-8") as fh:
            yaml.safe_dump({**config, "version": "0.25.1", "privileged": ["SYS_ADMIN"]}, fh)
        await env.sv.reload_store()
        rows, _ = await self.lists()
        self.assertIn("It is not a definition this manager writes: someone changed hri_garage/", rows["garage"]["problem"])
        self.assertNotIn("does not match the manager's record", rows["garage"]["problem"])

    # ------------------------------------------------------------------ a stale store, a folder the store has not missed

    async def test_a_stale_store_is_reloaded_not_repaired(self):
        """F5: the folder holds the recorded definition, the store still offers an earlier reading of it: no
        self-contradicting text, no Repair; the periodic check reloads the store once."""
        env = self.env
        await self.create("garage")
        env.stub.store["local_hri_garage"]["version"] = "0.24.9"  # read before the folder was put back
        rows, _ = await self.lists()
        problem = rows["garage"]["problem"]
        self.assertIn("the Supervisor's store still offers 0.24.9, an earlier reading of hri_garage/, which holds the "
                      "manager's definition of 0.25.0 as recorded", problem)
        self.assertNotIn("the record says", problem)
        self.assertEqual(rows["garage"]["actions"], ["stop", "delete"])
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.stub.calls.clear()
        rows, _ = await self.lists()
        self.assertEqual([c for c in env.stub.calls if c[1] == "/store/reload"], [("POST", "/store/reload", {})])
        rows, _ = await self.lists()
        self.assertIsNone(rows["garage"]["problem"])

    async def test_a_folder_gone_before_the_store_noticed(self):
        """F4: an instance's folder removed, the store not reloaded yet: said so (not "a local app the manager did not
        create"); the start reloads the store, and the first check repairs it."""
        env = self.env
        await self.create("garage")
        shutil.rmtree(self.folder("garage"))
        _, others = await self.lists()
        self.assertIn("the Supervisor's store has not noticed yet", others["local_hri_garage"]["problem"])
        notes = await env.manager.startup()
        self.assertIn("garage: the folder is gone and the store had not noticed: reloaded it", notes)
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        await env.manager.auto_repair_check()
        await self.wait_jobs()
        self.assertTrue(os.path.isdir(self.folder("garage")))

    # ------------------------------------------------------------------ automatic repair of a rolled-back folder (E)

    async def roll_back(self):
        """Updated 0.25.0 -> 0.25.1, then a restore of only the local apps folder from before the update."""
        env = self.env
        await self.create("garage")
        self.back_up("local")
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.restore("local")
        self.assertEqual(self.config("garage")["version"], "0.25.0")
        self.assertTrue(env.stub._app_view("local_hri_garage")["update_available"])  # the older one, as an update

    async def settled(self):
        """Two checks a settle apart see the rolled-back folder (the first only notes it)."""
        env = self.env
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        rows, _ = await self.lists()
        self.assertIsNone(rows["garage"]["job"])
        env.manager._rolled_back_seen["garage"] = (env.manager._rolled_back_seen["garage"][0]
                                                   - instances.ROLLED_BACK_SETTLE - 1,
                                                   *env.manager._rolled_back_seen["garage"][1:])
        rows, _ = await self.lists()
        return rows

    async def test_a_rolled_back_folder_is_written_again_from_the_copy(self):
        env = self.env
        await self.roll_back()
        rows, _ = await self.lists()  # no automatic repair yet: what the row says
        self.assertIn("it holds the manager's definition of 0.25.0, the record says 0.25.1", rows["garage"]["problem"])
        self.assertNotIn("omeone changed", rows["garage"]["problem"])
        env.stub.calls.clear()
        env.stub.codeload_paths.clear()
        rows = await self.settled()
        self.assertEqual(rows["garage"]["job"]["action"], "repair")
        self.assertEqual(rows["garage"]["auto_repair"]["reason"], "rolled_back")
        await self.wait_jobs()
        (job,) = [j for j in env.manager.jobs.recent() if j.action == "repair"]
        self.assertEqual((job.state, job.user), ("succeeded", instances.AUTO_USER), job.as_dict())
        self.assertIn("from the manager's copy", " ".join(line["msg"] for line in job.lines))
        self.assertEqual(env.stub.codeload_paths, [])  # nothing downloaded
        self.assertEqual(env.changing_calls(), [])  # nothing installed or updated
        self.assertEqual(self.config("garage")["version"], "0.25.1")
        m = self.marker("garage")
        self.assertEqual(m["history"][-1]["event"], f"repaired (automatic) at {m['repaired_at']}")
        self.assertFalse(env.stub._app_view("local_hri_garage")["update_available"])
        rows, _ = await self.lists()
        self.assertIsNone(rows["garage"]["problem"])
        self.assertEqual(rows["garage"]["auto_repair"]["state"], "succeeded")

    async def test_only_when_two_checks_a_minute_apart_see_it(self):
        """F1a: a restore of a folder and its app passes through the rolled-back state: the first check only notes
        it, a second one sooner than ROLLED_BACK_SETTLE does nothing, and a check that no longer sees it starts the
        count again."""
        env = self.env
        await self.roll_back()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        for _ in range(3):
            rows, _ = await self.lists()
            self.assertIsNone(rows["garage"]["job"])
        first = env.manager._rolled_back_seen["garage"][0]
        env.stub.installed["local_hri_garage"]["version"] = "0.25.0"  # the app's restore: not rolled back any more
        await self.lists()
        self.assertNotIn("garage", env.manager._rolled_back_seen)
        env.stub.installed["local_hri_garage"]["version"] = "0.25.1"
        await self.lists()
        self.assertGreater(env.manager._rolled_back_seen["garage"][0], first)
        self.assertEqual([j for j in env.manager.jobs.recent() if j.action == "repair"], [])

    async def test_the_manager_s_start_repairs_a_rolled_back_folder(self):
        env = self.env
        await self.roll_back()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.manager.rolled_back_settle = 0
        await env.manager.auto_repair_check()  # notes it
        await env.manager.auto_repair_check()
        await self.wait_jobs()
        self.assertEqual(self.config("garage")["version"], "0.25.1")

    async def test_nothing_while_the_supervisor_is_not_running_normally(self):
        """F1b: while the Supervisor makes or restores a backup (freeze), nothing is repaired automatically, and the
        admin's Repair does not replace the folder either."""
        env = self.env
        await self.roll_back()
        env.stub.sv_state = "freeze"
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.manager.rolled_back_settle = 0
        for _ in range(3):
            env.manager._auto_checked = None
            rows, _ = await self.lists()
            self.assertIsNone(rows["garage"]["job"])
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("the Supervisor is not running normally (its state is 'freeze')", job["error"])
        self.assertEqual(self.config("garage")["version"], "0.25.0")
        env.stub.sv_state = "running"
        rows, _ = await self.lists()
        self.assertEqual(rows["garage"]["job"]["action"], "repair")
        await self.wait_jobs()
        self.assertEqual(self.config("garage")["version"], "0.25.1")

    async def test_the_supervisor_changing_the_app_during_a_repair_is_not_contained(self):
        """F1c: the Supervisor restores the app (another version) while Repair writes the folder: the version alone
        differs afterwards, a race, not tampering. Needs attention; never stopped nor uninstalled."""
        env = self.env
        await self.roll_back()
        wait_store = env.manager._wait_store

        async def meanwhile(*args, **kw):
            await wait_store(*args, **kw)
            env.stub.installed["local_hri_garage"]["version"] = "0.25.0"

        env.stub.calls.clear()
        with unittest.mock.patch.object(env.manager, "_wait_store", side_effect=meanwhile):
            job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("it changed the installed app meanwhile", job["error"])
        self.assertIn("local_hri_garage", env.stub.installed)
        self.assertEqual(env.changing_calls(), [])  # no stop, no uninstall
        entry = env.registry.get("garage")
        self.assertIsNone(entry.get("tampered"))
        self.assertIn("changed the installed app meanwhile", entry["needs_attention"]["reason"])
        rows, _ = await self.lists()
        self.assertIn("needs attention: the Supervisor now reports local_hri_garage 0.25.0", rows["garage"]["problem"])

    async def test_the_automatic_repair_never_downloads_nor_removes_without_a_copy(self):
        """F2: the copy gone between the check and the job: the folder stays, nothing is downloaded."""
        env = self.env
        await self.roll_back()
        managed = children.load_managed(env.local_apps, "garage", env.registry)
        shutil.rmtree(self.copy_dir("garage"))
        env.stub.codeload_paths.clear()
        job = jobs.Job("garage", "repair", instances.AUTO_USER)
        with self.assertRaises(jobs.JobFailed) as ctx:
            await env.manager._repair_foreign(job, managed, instances.AUTO_USER, offline=True)
        self.assertIn("the automatic repair never downloads", str(ctx.exception))
        self.assertEqual(self.config("garage")["version"], "0.25.0")
        self.assertEqual(env.stub.codeload_paths, [])

    async def test_no_automatic_repair_unless_provably_the_manager_s_own_older_definition(self):
        env = self.env
        await self.roll_back()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.manager.rolled_back_settle = 0
        app = env.stub.installed["local_hri_garage"]
        cases = {
            "no copy of the installed version": lambda: shutil.rmtree(self.copy_dir("garage")),
            "a definition the manager would not write": lambda: self._append(
                os.path.join(self.folder("garage"), "config.yaml"), "privileged:\n  - SYS_ADMIN\n"),
            "a file the manager does not write": lambda: self._append(os.path.join(self.folder("garage"), "run.sh"), "x"),
            # F2: the installed app's Bluetooth is not the record's
            "another access than the record": lambda: app["definition"].update(host_dbus=True),
        }
        for label, spoil in cases.items():
            with self.subTest(case=label):
                kept = os.path.join(self.saved, "case")
                shutil.copytree(env.data, kept, symlinks=True)
                spoil()
                for _ in range(2):
                    env.manager._auto_checked = None
                    rows, _ = await self.lists()
                self.assertIsNone(rows["garage"]["job"])
                self.assertEqual(rows["garage"]["actions"], ["repair", "stop", "delete"])
                self.assertEqual(self.config("garage")["version"], "0.25.0")
                app["definition"]["host_dbus"] = False
                shutil.rmtree(env.data)
                shutil.copytree(kept, env.data, symlinks=True)
                shutil.rmtree(kept)
                await self.restore("local")

    async def test_put_new_never_replaces_an_entry(self):
        env = self.env
        env.registry.put_new("attic", {"name": "attic"})
        with self.assertRaises(registry.RegistryError):
            env.registry.put_new("attic", {"name": "other"})
        self.assertEqual(env.registry.get("attic"), {"name": "attic"})

    @staticmethod
    def _append(path, text):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    unittest.main()
