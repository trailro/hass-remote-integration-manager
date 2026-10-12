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

import yaml

from hrimgr import children, instances

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
        self.assertEqual((other["kind"], other["actions"], other["instance"]), ("folder", ["adopt"], "attic"))
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

    # ------------------------------------------------------------------ wording (D2) and the registry's history

    async def test_a_registry_behind_the_folder_is_said_neutrally_and_kept_in_the_history(self):
        """D2: updated after the manager's backup, then a restore of only the manager: its folder is the manager's own
        definition of a version its record does not have.  Said in neutral words; never repaired by itself; Repair
        writes it, and its history says the record was behind."""
        env = self.env
        await self.create("garage")
        self.back_up("data")
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.restore("data")
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        rows, _ = await self.lists()
        row = rows["garage"]
        self.assertIsNone(row["job"])  # not repaired automatically: the record is not at the installed version
        self.assertIn("hri_garage/ does not match the manager's record: it holds the manager's definition of 0.25.1, "
                      "the record says 0.25.0", row["problem"])
        self.assertIn("after restoring only part of a backup", row["problem"])
        self.assertNotIn("omeone changed", row["problem"])
        self.assertEqual(row["actions"], ["repair", "stop", "delete"])
        status, answer = await env.send("POST", "/api/instances/garage/restart")
        self.assertEqual(status, 400)
        self.assertIn("does not match the manager's record", answer["error"])
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        entry = env.registry.get("garage")
        self.assertEqual(entry["version"], "0.25.1")
        last = entry["history"][-1]
        self.assertEqual(last["version"], "0.25.0")
        self.assertIn("repaired (manual) at", last["event"])
        self.assertIn("the manager's record was behind the installed app (0.25.0 -> 0.25.1)", last["event"])
        rows, _ = await self.lists()
        self.assertIsNone(rows["garage"]["problem"])

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

    async def test_a_rolled_back_folder_is_written_again_from_the_copy(self):
        env = self.env
        await self.roll_back()
        rows, _ = await self.lists()  # no automatic repair yet: what the row says
        self.assertIn("it holds the manager's definition of 0.25.0, the record says 0.25.1", rows["garage"]["problem"])
        self.assertNotIn("omeone changed", rows["garage"]["problem"])
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.stub.calls.clear()
        env.stub.codeload_paths.clear()
        rows, _ = await self.lists()
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

    async def test_the_manager_s_start_repairs_a_rolled_back_folder(self):
        env = self.env
        await self.roll_back()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        await env.manager.auto_repair_check()
        await self.wait_jobs()
        self.assertEqual(self.config("garage")["version"], "0.25.1")

    async def test_no_automatic_repair_unless_provably_the_manager_s_own_older_definition(self):
        env = self.env
        await self.roll_back()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        cases = {
            "no copy of the installed version": lambda: shutil.rmtree(self.copy_dir("garage")),
            "a definition the manager would not write": lambda: self._append(
                os.path.join(self.folder("garage"), "config.yaml"), "privileged:\n  - SYS_ADMIN\n"),
            "a file the manager does not write": lambda: self._append(os.path.join(self.folder("garage"), "run.sh"), "x"),
        }
        for label, spoil in cases.items():
            with self.subTest(case=label):
                kept = os.path.join(self.saved, "case")
                shutil.copytree(env.data, kept, symlinks=True)
                spoil()
                env.manager._auto_checked = None
                rows, _ = await self.lists()
                self.assertIsNone(rows["garage"]["job"])
                self.assertEqual(rows["garage"]["actions"], ["repair", "stop", "delete"])
                self.assertEqual(self.config("garage")["version"], "0.25.0")
                shutil.rmtree(env.data)
                shutil.copytree(kept, env.data, symlinks=True)
                shutil.rmtree(kept)
                await self.restore("local")

    @staticmethod
    def _append(path, text):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    unittest.main()
