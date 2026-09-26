"""Automatic repair after a restore: a full backup leaves the local apps folder out (the Supervisor logs "Can't find
backup folder addons/local"), so after a restore the instances are installed and detached.  At the manager's start,
and on a list at most every few minutes, each instance of the registry in that state gets its definition written
again; nothing outside the registry is touched."""

import asyncio
import os
import shutil
import unittest
from unittest import mock

from hrimgr import instances
from hrimgr.github import GitHubError

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
        """GitHub unreachable: shown, and tried again at a later check."""
        env = self.env
        await self.create("garage")
        await self.restore_without_the_local_apps_folder()
        shutil.rmtree(os.path.join(env.data, "definitions", "garage"))  # no copy: GitHub, which is down
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        with mock.patch.object(env.gh, "_get", side_effect=GitHubError("GitHub unreachable: ClientConnectorError")):
            await env.get("/api/instances")
            await self.wait_jobs()
        _, data = await env.get("/api/instances")
        row = data["instances"][0]
        self.assertEqual((row["auto_repair"]["state"], row["actions"]), ("failed", ["repair"]))
        self.assertIn("unreachable", row["auto_repair"]["error"])
        self.assertGreater(row["auto_repair"]["next_try_in"], 0)
        env.manager._auto_checked -= instances.AUTO_REPAIR_INTERVAL + 1
        env.manager.auto_backoff["garage"]["next"] = 0  # its back-off has passed too
        await env.get("/api/instances")
        await self.wait_jobs()
        self.assertTrue(os.path.isdir(self.folder("garage")))

    async def test_repeated_failures_back_off_and_log_once(self):
        """GitHub down for long: each instance waits twice as long after each failure, up to a day; the first failure
        is a warning, the repeats debug lines; the row shows the last failure and when the next try is."""
        env = self.env
        await self.create("garage")
        await self.restore_without_the_local_apps_folder()
        shutil.rmtree(os.path.join(env.data, "definitions", "garage"))
        manager = env.manager
        manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL

        async def check():
            manager._auto_checked = None  # the global cadence is not what this test is about
            await env.get("/api/instances")
            await self.wait_jobs()
            return len([j for j in manager.jobs.recent() if j.action == "repair"])

        down = mock.patch.object(env.gh, "_get", side_effect=GitHubError("GitHub unreachable: ClientConnectorError"))
        down.start()
        self.addCleanup(mock.patch.stopall)
        with self.assertLogs("hrimgr.instances", level="DEBUG") as logs:
            self.assertEqual(await check(), 1)
            self.assertEqual(await check(), 1)  # within its back-off: not tried
            delays = []
            for attempt in (2, 3, 4):
                manager.auto_backoff["garage"]["next"] = 0  # the back-off has passed
                self.assertEqual(await check(), attempt)
                delays.append(manager.auto_backoff["garage"]["delay"])
        self.assertEqual(delays, [instances.AUTO_REPAIR_INTERVAL * 2, instances.AUTO_REPAIR_INTERVAL * 4,
                                  instances.AUTO_REPAIR_INTERVAL * 8])
        warnings = [r for r in logs.records if r.levelname == "WARNING" and "garage" in r.getMessage()]
        self.assertEqual(len(warnings), 2, [r.getMessage() for r in warnings])  # the first start, the first failure
        manager.auto_backoff["garage"].update(failures=30, next=0)
        await check()
        self.assertEqual(manager.auto_backoff["garage"]["delay"], instances.AUTO_REPAIR_MAX_DELAY)
        _, data = await env.get("/api/instances")
        note = data["instances"][0]["auto_repair"]
        self.assertEqual(note["state"], "failed")
        self.assertIn("unreachable", note["error"])
        self.assertEqual(note["failures"], 31)
        self.assertGreater(note["next_try_in"], 0)
        down.stop()
        manager.auto_backoff["garage"]["next"] = 0
        await check()
        self.assertTrue(os.path.isdir(self.folder("garage")))
        self.assertNotIn("garage", manager.auto_backoff)  # a success forgets the failures

    async def test_never_another_version_and_not_again_when_it_needs_attention(self):
        """The installed commit of a git instance is gone: automatic repair writes nothing (not the branch's head), and
        leaves the instance to the user from then on."""
        env = self.env
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        env.stub.commits.discard(env.stub.refs["main"])
        env.stub.refs["main"] = "f" * 40
        await self.restore_without_the_local_apps_folder()
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        await env.get("/api/instances")
        await self.wait_jobs()
        self.assertFalse(os.path.lexists(self.folder("lab")))
        self.assertNotIn("local_hri_lab", env.stub.store)
        self.assertNotIn("refs/heads/main", env.stub.codeload_paths[-1:])
        _, data = await env.get("/api/instances")
        self.assertTrue(data["instances"][0]["needs_attention"])
        env.manager._auto_checked -= instances.AUTO_REPAIR_INTERVAL + 1
        await env.get("/api/instances")
        self.assertEqual(len([j for j in env.manager.jobs.recent() if j.action == "repair"]), 1)

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
