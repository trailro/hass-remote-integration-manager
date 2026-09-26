"""Automatic repair after a restore: a full backup leaves the local apps folder out (the Supervisor logs "Can't find
backup folder addons/local"), so after a restore the instances are installed and detached.  At the manager's start,
and on a list at most every few minutes, each instance of the registry in that state gets its definition written
again; nothing outside the registry is touched."""

import asyncio
import os
import shutil
import unittest

from hrimgr import instances

from .env import Env
from .helpers import tmpdir

FOREIGN_URL = "https://github.com/trailro/hass-remote-integration"


class AutoRepairTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()

    async def asyncTearDown(self):
        self.env.assert_only_allowed_calls(self)
        await self.env.close()

    def folder(self, name):
        return os.path.join(self.env.local_apps, f"hri_{name}")

    async def create(self, name, **body):
        body = {"name": name, "channel": "release", "version": "0.25.0", **body}
        job = await self.env.job(await self.env.send("POST", "/api/instances", body))
        self.assertEqual(job["state"], "succeeded", job)

    async def restore_without_the_local_apps_folder(self):
        """What a full restore does to the local apps folder on current Supervisors; plus a detached app someone
        made by hand, with an instance's slug and HRI's url, that the registry does not hold."""
        env = self.env
        for entry in os.listdir(env.local_apps):
            shutil.rmtree(os.path.join(env.local_apps, entry))
        env.stub.installed["local_hri_handmade"] = {"slug": "local_hri_handmade", "name": "Hand-made", "version": "0.25.0",
                                                    "state": "started", "url": FOREIGN_URL, "repository": "local"}
        await env.sv.reload_store()

    async def wait_jobs(self):
        for _ in range(500):
            if not any(j.state == "running" for j in self.env.manager.jobs.recent()):
                return
            await asyncio.sleep(0.02)
        raise AssertionError("jobs still running")

    async def test_a_list_repairs_the_registry_s_detached_instances(self):
        env = self.env
        await self.create("garage")
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        await self.restore_without_the_local_apps_folder()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        env.stub.calls.clear()

        _, data = await env.get("/api/instances")
        rows = {i["name"]: i for i in data["instances"]}
        self.assertEqual(sorted(rows), ["garage", "lab"])
        for name in ("garage", "lab"):
            self.assertEqual(rows[name]["job"]["action"], "repair")
            self.assertEqual(rows[name]["auto_repair"]["state"], "running")
        self.assertIn("local_hri_handmade", [o["slug"] for o in data["others"]])
        await self.wait_jobs()

        jobs = [j for j in env.manager.jobs.recent() if j.action == "repair"]
        self.assertEqual(sorted(j.instance for j in jobs), ["garage", "lab"])
        for job in jobs:
            self.assertEqual((job.state, job.user), ("succeeded", instances.AUTO_USER), job.as_dict())
            self.assertIn("started automatically", job.lines[0]["msg"])
        for name in ("garage", "lab"):
            self.assertTrue(os.path.isfile(os.path.join(self.folder(name), "config.yaml")))
        _, data = await env.get("/api/instances")
        rows = {i["name"]: i for i in data["instances"]}
        for name in ("garage", "lab"):
            self.assertTrue(rows[name]["managed"])
            self.assertEqual(rows[name]["auto_repair"]["state"], "succeeded")
        # the hand-made app: no folder, no registry entry, no call that changes it
        self.assertFalse(os.path.lexists(self.folder("handmade")))
        self.assertIsNone(env.registry.get("handmade"))
        self.assertEqual(env.changing_calls("local_hri_handmade"), [])
        self.assertEqual(env.changing_calls(), [])  # a repair writes files and reloads the store: nothing else

    async def test_at_most_every_few_minutes(self):
        env = self.env
        await self.create("garage")
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        await env.get("/api/instances")  # the first check: nothing to repair
        await self.restore_without_the_local_apps_folder()
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"][0]["actions"], ["repair"])  # within the interval: offered, not started
        self.assertIsNone(data["instances"][0]["job"])
        env.manager._auto_checked -= instances.AUTO_REPAIR_INTERVAL + 1
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"][0]["job"]["action"], "repair")
        await self.wait_jobs()
        self.assertTrue(os.path.isdir(self.folder("garage")))

    async def test_a_failed_automatic_repair_is_shown_and_tried_again(self):
        env = self.env
        await self.create("garage")
        await self.restore_without_the_local_apps_folder()
        shutil.rmtree(os.path.join(env.data, "definitions", "garage"))  # no copy: GitHub, which fails
        env.stub.releases.remove("0.25.0")
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        await env.get("/api/instances")
        await self.wait_jobs()
        _, data = await env.get("/api/instances")
        row = data["instances"][0]
        self.assertEqual((row["auto_repair"]["state"], row["actions"]), ("failed", ["repair"]))
        self.assertIn("not a published release", row["auto_repair"]["error"])
        env.stub.releases.append("0.25.0")
        env.manager._auto_checked -= instances.AUTO_REPAIR_INTERVAL + 1
        await env.get("/api/instances")
        await self.wait_jobs()
        self.assertTrue(os.path.isdir(self.folder("garage")))

    async def test_at_the_manager_s_start(self):
        env = self.env
        await self.create("garage")
        await self.restore_without_the_local_apps_folder()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        await env.manager.auto_repair_check()
        await self.wait_jobs()
        self.assertTrue(os.path.isdir(self.folder("garage")))
        self.assertEqual([j.user for j in env.manager.jobs.recent() if j.action == "repair"], [instances.AUTO_USER])

    async def test_the_start_copies_the_definitions_it_has_no_copy_of(self):
        """Instances created by 0.1.0 have no copy in /data: the manager makes one at its start (managed ones only)."""
        env = self.env
        await self.create("garage")
        copy_dir = os.path.join(env.data, "definitions", "garage")
        shutil.rmtree(copy_dir)
        os.makedirs(os.path.join(env.local_apps, "hri_stranger"))  # a folder the registry does not hold
        with open(os.path.join(env.local_apps, "hri_stranger", "config.yaml"), "w") as fh:
            fh.write("slug: hri_stranger\nname: x\nversion: '1'\n")
        await env.manager.auto_repair_check()
        self.assertEqual(sorted(os.listdir(copy_dir)), ["CHANGELOG.md", "DOCS.md", "config.yaml", "copy.json", "translations"])
        self.assertEqual(os.listdir(os.path.join(env.data, "definitions")), ["garage"])

    async def test_off_when_no_interval(self):
        env = self.env
        await self.create("garage")
        await self.restore_without_the_local_apps_folder()
        await env.manager.auto_repair_check()
        self.assertEqual([j for j in env.manager.jobs.recent() if j.action == "repair"], [])


if __name__ == "__main__":
    unittest.main()
