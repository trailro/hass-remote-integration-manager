"""Create, update, start/stop/restart, delete and repair through the API, against the fake Supervisor and GitHub over
real HTTP; rollbacks on failure; and the marker and slug checks in front of every changing action."""

import asyncio
import datetime
import json
import threading
import os
import shutil
import time
import unittest
from unittest import mock

import yaml

from hrimgr import VERSION, children, copies, stamp
from hrimgr.jobs import Job, JobFailed
from hrimgr.registry import RegistryError
from hrimgr.supervisor import SupervisorError

from .env import Env
from .fakes.tarballs import sha_of
from .helpers import FIXTURE_0252, marker, register, tmpdir


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()

    async def asyncTearDown(self):
        self.env.assert_only_allowed_calls(self)
        await self.env.close()

    def folder(self, name):
        return os.path.join(self.env.local_apps, f"hri_{name}")

    def config(self, name):
        with open(os.path.join(self.folder(name), "config.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def copy_dir(self, name):
        return os.path.join(self.env.data, "definitions", name)

    def read_tree(self, path):
        out = {}
        for folder, _, files in os.walk(path):
            for f in files:
                full = os.path.join(folder, f)
                with open(full, "rb") as fh:
                    out[os.path.relpath(full, path)] = fh.read()
        return out

    def marker(self, name):
        with open(os.path.join(self.folder(name), children.MARKER), encoding="utf-8") as fh:
            return json.load(fh)

    async def create(self, name="garage", **body):
        body = {"name": name, "channel": "release", "version": "0.25.0", **body}
        return await self.env.job(await self.env.send("POST", "/api/instances", body))

    async def test_release_lifecycle(self):
        env = self.env
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["user"], "alice")
        app = env.stub.installed["local_hri_garage"]
        self.assertEqual((app["version"], app["state"], app["boot"], app["watchdog"], app["ingress_panel"]),
                         ("0.25.0", "started", "auto", True, True))
        config = self.config("garage")
        self.assertEqual((config["slug"], config["version"], config["name"]), ("hri_garage", "0.25.0", "HRI Garage"))
        self.assertEqual(config["image"], "ghcr.io/trailro/hass-remote-integration")
        m = self.marker("garage")
        self.assertEqual((m["channel"], m["version"], m["ref"], m["sha"], m["created_by"]), ("release", "0.25.0", "v0.25.0", sha_of("tag-0.25.0"), "alice"))
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])

        status, data = await env.get("/api/instances")
        self.assertEqual(status, 200)
        (inst,) = data["instances"]
        self.assertEqual((inst["name"], inst["state"], inst["channel"], inst["installed_version"]), ("garage", "started", "release", "0.25.0"))
        self.assertTrue(inst["ingress_panel"])
        self.assertTrue(inst["ingress_url"].startswith("/api/hassio_ingress/"))
        self.assertEqual(inst["actions"], ["restart", "stop", "update", "delete"])
        kinds = {o["slug"]: o["kind"] for o in data["others"]}
        self.assertEqual(kinds, {"5c53de3b_hass_remote_integration": "published", "local_hri_foreign": "local"})

        await env.get("/api/releases")
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"][0]["newer_release"], "0.25.1")  # the newest stable, not the pre-release

        for action, state in (("stop", "stopped"), ("start", "started"), ("restart", "started")):
            job = await env.job(await env.send("POST", f"/api/instances/garage/{action}"))
            self.assertEqual((job["state"], job["result"]["state"]), ("succeeded", state))

        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.25.1")
        self.assertEqual(self.config("garage")["version"], "0.25.1")
        m = self.marker("garage")
        self.assertEqual(m["version"], "0.25.1")
        self.assertEqual([h["version"] for h in m["history"]], ["0.25.0"])
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])  # the previous definition is gone

        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.0"}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("does not downgrade", job["error"])

        job = await env.job(await env.send("DELETE", "/api/instances/garage", {"remove_data": False, "confirm": "garage"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertIn("local_hri_garage", env.stub.kept_data)
        self.assertEqual(os.listdir(env.local_apps), [])
        self.assertIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)

    async def test_git_channel(self):
        env = self.env
        job = await self.create("lab", channel="git", ref_kind="branch", ref="main")
        self.assertEqual(job["state"], "succeeded", job)
        sha = env.stub.refs["main"]
        self.assertEqual(env.stub.installed["local_hri_lab"]["version"], f"0.0.0-{sha[:12]}")
        self.assertTrue(env.stub.installed["local_hri_lab"]["build"])
        config = self.config("lab")
        self.assertNotIn("image", config)
        self.assertEqual(config["slug"], "hri_lab")
        self.assertFalse(os.path.exists(os.path.join(self.folder("lab"), "app", "config.yaml")))
        self.assertTrue(os.path.isfile(os.path.join(self.folder("lab"), "custom_components/integration_manager/static/config.js")))
        with open(os.path.join(self.folder("lab"), "Dockerfile"), encoding="utf-8") as fh:
            self.assertIn(f"ARG HRI_BUILD={sha}", fh.read())
        self.assertEqual(sorted(env.stub.store), ["local_hri_lab"])  # one app, not the tree's decoys

        job = await env.job(await env.send("POST", "/api/instances/lab/update", {}))
        self.assertEqual((job["state"], job["result"].get("unchanged")), ("succeeded", True))

        env.stub.refs["main"] = sha_of("main-2")
        job = await env.job(await env.send("POST", "/api/instances/lab/update", {}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.installed["local_hri_lab"]["version"], f"0.0.0-{sha_of('main-2')[:12]}")
        self.assertEqual(self.marker("lab")["history"][0]["sha"], sha)

        job = await env.job(await env.send("DELETE", "/api/instances/lab", {"remove_data": True, "confirm": "lab"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIn(("POST", "/addons/local_hri_lab/uninstall", {"remove_config": True}), env.stub.calls)
        self.assertEqual(set(env.stub.codeload_paths), {"refs/heads/main"})  # always the full ref

    async def test_a_new_commit_with_the_same_version_is_refused(self):
        """Two commits whose versions are equal (the same leading hex): an update that would be a silent no-op fails."""
        env = self.env
        env.stub.refs["main"] = "0123456789ab" + "0" * 28
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        self.assertEqual(env.stub.installed["local_hri_lab"]["version"], "0.0.0-0123456789ab")
        env.stub.refs["main"] = "0123456789ab" + "1" * 28
        job = await env.job(await env.send("POST", "/api/instances/lab/update", {}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("same version", job["error"])
        self.assertEqual(self.marker("lab")["sha"], "0123456789ab" + "0" * 28)

    async def test_git_channel_takes_only_branches_and_tags_of_hri(self):
        env = self.env
        for kind, ref in (("branch", "pull/1/head"), ("branch", "refs/pull/1/head"), ("branch", "a" * 40), ("branch", "abcdef1"),
                          ("commit", "main"), (None, "main"), ("branch", "refs/heads/main")):
            with self.subTest(kind=kind, ref=ref):
                status, data = await env.send("POST", "/api/instances", {"name": "lab", "channel": "git", "ref_kind": kind, "ref": ref})
                self.assertEqual(status, 400, data)
        job = await self.create("lab", channel="git", ref_kind="branch", ref="no-such-branch")
        self.assertEqual(job["state"], "failed")
        self.assertIn("has no branch no-such-branch", job["error"])
        job = await self.create("lab", channel="git", ref_kind="branch", ref="test-tag")  # a tag, asked as a branch
        self.assertEqual(job["state"], "failed")
        self.assertEqual(env.stub.codeload_paths, [])  # nothing downloaded before the ref was found in HRI's repository
        job = await self.create("lab", channel="git", ref_kind="tag", ref="test-tag")
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.codeload_paths, ["refs/tags/test-tag"])
        m = self.marker("lab")
        self.assertEqual((m["ref_kind"], m["ref"], m["sha"]), ("tag", "test-tag", env.stub.tags["test-tag"]))
        status, _ = await env.send("POST", "/api/instances/lab/update", {"ref_kind": "branch", "ref": "pull/2/head"})
        self.assertEqual(status, 400)

    async def test_a_failed_install_is_rolled_back(self):
        env = self.env
        env.stub.fail[("POST", "/addons/local_hri_garage/start")] = "Can't start: port in use"
        job = await self.create()
        self.assertEqual(job["state"], "failed")
        self.assertIn("port in use", job["error"])
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertEqual(os.listdir(env.local_apps), [])
        self.assertIn(("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}), env.stub.calls)
        self.assertNotIn("local_hri_garage", env.stub.store)

    async def test_a_failed_info_after_a_successful_start_keeps_the_instance(self):
        env = self.env
        finish = env.manager._finish_setup

        async def then_fail(job, managed):  # the info after the start; the one checking the install succeeds
            await finish(job, managed)
            env.stub.fail[("GET", "/addons/local_hri_garage/info")] = "Supervisor busy"

        env.manager._finish_setup = then_fail
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.installed["local_hri_garage"]["state"], "started")
        self.assertTrue(os.path.isdir(self.folder("garage")))
        self.assertTrue(env.registry.get("garage")["setup_complete"])

    async def _cancel_when(self, job_id: str, line: str, call: str | None = None) -> dict:
        """Cancel a job (as the manager's stop does) once its log shows ``line`` and, when given, the fake Supervisor
        has received the POST ``call``; its final state."""
        job = self.env.manager.jobs.get(job_id)
        for _ in range(500):
            if any(line in l["msg"] for l in job.lines) and (
                    call is None or any(m == "POST" and p == call for m, p, _ in self.env.stub.calls)):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(f"the job never logged {line!r} (or the call {call} never came): {job.lines}")
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
        return job.as_dict()

    async def test_a_create_stopped_while_starting_is_rolled_back(self):
        env = self.env
        env.stub.delay = 0.3
        status, body = await env.send("POST", "/api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"})
        job = await self._cancel_when(body["job"]["id"], "starting")
        self.assertEqual(job["state"], "failed")
        self.assertIn("cancelled", job["error"])
        self.assertTrue(any("rolling back" in l["msg"] for l in job["lines"]), job["lines"])
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertEqual(os.listdir(env.local_apps), [])
        self.assertIsNone(env.registry.get("garage"))

    async def test_a_create_stopped_while_installing_says_install_interrupted(self):
        """The Supervisor finishes an install the manager stopped waiting for: a detached app, labelled as such."""
        env = self.env
        env.stub.delay = 0.3
        status, body = await env.send("POST", "/api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"})
        await self._cancel_when(body["job"]["id"], "installing", "/store/addons/local_hri_garage/install")
        self.assertEqual(os.listdir(env.local_apps), [])
        for _ in range(100):  # the stub completes the install it was asked for
            if "local_hri_garage" in env.stub.installed:
                break
            await asyncio.sleep(0.02)
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        (inst,) = data["instances"]
        self.assertIn("install interrupted", inst["problem"])
        self.assertNotIn("partial restore", inst["problem"])
        self.assertEqual(inst["actions"], ["repair"])
        env.stub.delay = 0
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        _, data = await env.get("/api/instances")
        self.assertIn("finish", data["instances"][0]["actions"])  # its boot, Watchdog and panel were never set

    async def test_an_install_call_without_a_clean_answer_keeps_the_instance(self):
        """A timeout or a lost connection is no refusal: the Supervisor installs as a task of its own and may finish."""
        env = self.env
        install = env.sv.install

        async def times_out(managed):
            env.stub.delay = 0.2
            asyncio.get_running_loop().create_task(install(managed))  # the Supervisor goes on
            await asyncio.sleep(0.05)
            raise SupervisorError(f"POST /store/addons/{managed.slug}/install: no answer from the Supervisor in 3600 s")

        with mock.patch.object(env.sv, "install", side_effect=times_out):
            job = await self.create()
        self.assertEqual(job["state"], "failed")
        self.assertIn("may still be installing it", job["error"])
        entry = env.registry.get("garage")
        self.assertTrue(entry["interrupted"])
        self.assertEqual(os.listdir(env.local_apps), [])
        for _ in range(100):
            if "local_hri_garage" in env.stub.installed:
                break
            await asyncio.sleep(0.02)
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        (inst,) = data["instances"]
        self.assertIn("install interrupted", inst["problem"])
        self.assertEqual(inst["actions"], ["repair"])

    async def test_forget_waits_for_an_install_that_may_still_finish(self):
        env = self.env

        async def lost(managed):
            raise SupervisorError(f"POST /store/addons/{managed.slug}/install: ServerDisconnectedError")

        with mock.patch.object(env.sv, "install", side_effect=lost):
            job = await self.create()
        self.assertEqual(job["state"], "failed")
        self.assertTrue(env.registry.get("garage")["interrupted"])
        _, data = await env.get("/api/instances")
        (orphan,) = [o for o in data["others"] if o.get("instance") == "garage"]
        self.assertEqual(orphan["actions"], [])
        self.assertIn("may still finish it", orphan["problem"])
        status, body = await env.send("POST", "/api/instances/garage/forget", {"confirm": "garage"})
        self.assertEqual(status, 400)
        self.assertIn("Forget is refused for 60 minutes", body["error"])
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        env.registry.update("garage", interrupted_at=long_ago.replace(microsecond=0).isoformat())
        job = await env.job(await env.send("POST", "/api/instances/garage/forget", {"confirm": "garage"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIsNone(env.registry.get("garage"))

    async def test_a_build_the_builder_refuses_leaves_no_registry_entry(self):
        env = self.env
        for what, patch in (("a second app", mock.patch.object(stamp, "find_configs", return_value=["config.yaml", "docs/config.yaml"])),
                            ("stamp's own refusal", mock.patch.object(stamp, "build_release", side_effect=ValueError("unknown channel 'x'")))):
            with self.subTest(what=what), patch:
                job = await self.create()
                self.assertEqual(job["state"], "failed", job)
                self.assertIn("the definition was not written", job["error"])
                self.assertIsNone(env.registry.get("garage"))
                self.assertEqual(os.listdir(env.local_apps), [])

    async def test_an_install_the_supervisor_refuses_is_rolled_back_and_forgotten(self):
        env = self.env
        env.stub.fail[("POST", "/store/addons/local_hri_garage/install")] = "Not enough free space"
        job = await self.create()
        self.assertEqual(job["state"], "failed")
        self.assertNotIn("may still be installing", job["error"])
        self.assertIsNone(env.registry.get("garage"))
        self.assertEqual(os.listdir(env.local_apps), [])

    async def test_an_update_stopped_midway_puts_the_previous_definition_back(self):
        env = self.env
        await self.create()
        env.stub.delay = 0.3
        never = asyncio.Event()

        async def before_the_call(*args):  # stopped before the update call is made: nothing to wait for
            await never.wait()

        with mock.patch.object(env.manager, "_verify_store", side_effect=before_the_call):
            status, body = await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"})
            job = await self._cancel_when(body["job"]["id"], "the store has local_hri_garage 0.25.1")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.config("garage")["version"], "0.25.0")
        self.assertEqual(self.marker("garage")["version"], "0.25.0")
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])

    async def test_an_update_killed_before_the_supervisor_applied_it_is_put_back_at_the_next_start(self):
        """A hard kill (OOM, power, the Supervisor's SIGKILL) while the update call was on its way, which the
        Supervisor then did not apply: no rollback runs; the next start sees the old version installed and puts the
        previous definition back."""
        env = self.env
        await self.create()
        never = asyncio.Event()

        async def hangs(managed):
            await never.wait()

        with mock.patch.object(type(env.manager), "_rollback_update", new=mock.AsyncMock()), \
                mock.patch.object(env.sv, "update", side_effect=hangs):
            status, body = await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"})
            await self._cancel_when(body["job"]["id"], "updating")
        self.assertEqual(self.config("garage")["version"], "0.25.1")  # as the kill left it
        self.assertEqual(len(os.listdir(env.local_apps)), 2)
        self.assertIsInstance(env.registry.get("garage")["updating"], dict)
        done = await env.manager.startup()  # what the next start runs first
        self.assertIn("the Supervisor has not installed it", " ".join(done))
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertEqual(self.config("garage")["version"], "0.25.0")
        self.assertEqual(self.marker("garage")["version"], "0.25.0")
        entry = env.registry.get("garage")
        self.assertEqual((entry["version"], entry["updating"]), ("0.25.0", None))

    async def test_an_update_the_supervisor_finished_after_a_kill_is_kept_never_downgraded(self):
        """The kill came after the Supervisor had the call, and it finished the update: putting the previous definition
        back would make it offer 0.25.0 over the installed 0.25.1, and install it with auto-update on."""
        env = self.env
        await self.create()
        env.stub.delay = 0.3
        with mock.patch.object(type(env.manager), "_rollback_update", new=mock.AsyncMock()):
            status, body = await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"})
            await self._cancel_when(body["job"]["id"], "updating", "/store/addons/local_hri_garage/update")
        for _ in range(100):
            if env.stub.installed["local_hri_garage"]["version"] == "0.25.1":
                break
            await asyncio.sleep(0.02)
        await env.manager.startup()
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertEqual(self.config("garage")["version"], "0.25.1")
        entry = env.registry.get("garage")
        self.assertEqual((entry["version"], entry["updating"]), ("0.25.1", None))
        self.assertTrue(entry["tampered"]["pending"])  # checked by the manager before anything else is done with it
        await env.manager.jobs.wait_all()  # the start's check, as a job
        self.assertIsNone(env.registry.get("garage")["tampered"])
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        self.assertFalse(data["instances"][0]["update_available"])  # no downgrade offered

    async def test_an_update_call_that_fails_after_the_supervisor_applied_it_keeps_the_new_definition(self):
        env = self.env
        await self.create()
        update = env.sv.update

        async def applied_then_lost(managed):
            await update(managed)
            raise SupervisorError(f"POST /store/addons/{managed.slug}/update: no answer from the Supervisor in 3600 s")

        with mock.patch.object(env.sv, "update", side_effect=applied_then_lost):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("The Supervisor has installed 0.25.1 all the same", job["error"])
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertEqual(self.config("garage")["version"], "0.25.1")
        self.assertEqual(env.registry.get("garage")["version"], "0.25.1")

    async def test_an_update_call_without_an_answer_leaves_both_definitions_for_the_next_start(self):
        env = self.env
        await self.create()

        app_info, down = env.sv.app_info, []

        async def lost(managed):  # no answer, and the Supervisor stays unreachable for a while
            down.append(True)
            raise SupervisorError(f"POST /store/addons/{managed.slug}/update: ServerDisconnectedError")

        async def info(slug):
            if down:
                raise SupervisorError(f"GET /addons/{slug}/info: ServerDisconnectedError")
            return await app_info(slug)

        with mock.patch.object(env.sv, "update", side_effect=lost), mock.patch.object(env.sv, "app_info", side_effect=info):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("is not known yet", job["error"])
        self.assertEqual(len(os.listdir(env.local_apps)), 2)
        await env.manager.startup()  # the Supervisor still has 0.25.0: the previous definition comes back
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertEqual(self.config("garage")["version"], "0.25.0")

    async def test_a_registry_error_after_the_update_is_a_warning(self):
        env = self.env
        await self.create()
        update = env.registry.update

        def full_disk(name, **fields):
            if "stamp_version" in fields:
                raise RegistryError("the manager's registry cannot be written: No space left on device")
            return update(name, **fields)

        with mock.patch.object(env.registry, "update", side_effect=full_disk):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIn("updated to 0.25.1, but the manager's registry was not updated", job["result"]["warning"])
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.25.1")
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertEqual(env.registry.get("garage")["version"], "0.25.0")
        await env.manager.startup()  # the next start catches up
        entry = env.registry.get("garage")
        self.assertEqual((entry["version"], entry["updating"]), ("0.25.1", None))

    async def test_a_previous_definition_that_cannot_be_removed_is_a_warning_and_goes_at_the_next_start(self):
        env = self.env
        await self.create()
        with mock.patch.object(children.Replacement, "commit", side_effect=PermissionError(13, "Permission denied")):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIn("the previous definition was not removed", job["result"]["warning"])
        self.assertIn("the next start removes it", job["result"]["warning"])
        entry = env.registry.get("garage")
        self.assertEqual((entry["version"], entry["updating"]), ("0.25.1", None))  # recorded all the same
        self.assertEqual(len(os.listdir(env.local_apps)), 2)
        children.cleanup_stale(env.local_apps, env.registry)
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])
        self.assertEqual(self.config("garage")["version"], "0.25.1")

    async def test_a_later_failed_update_does_not_lose_an_earlier_update_s_record(self):
        env = self.env
        await self.create()
        update = env.registry.update

        def full_disk(name, **fields):
            if "stamp_version" in fields and fields.get("updating", 1) is None:
                raise RegistryError("the manager's registry cannot be written: No space left on device")
            return update(name, **fields)

        with mock.patch.object(env.registry, "update", side_effect=full_disk):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertIn("warning", job["result"])
        self.assertEqual(env.registry.get("garage")["version"], "0.25.0")  # behind, its flag still set
        env.stub.releases.append("0.25.3")
        env.stub.fail[("POST", "/store/addons/local_hri_garage/update")] = "pull failed"
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.3"}))
        self.assertEqual(job["state"], "failed")
        entry = env.registry.get("garage")
        self.assertEqual((entry["version"], entry["updating"]), ("0.25.1", None))  # the first update is recorded
        self.assertEqual(self.config("garage")["version"], "0.25.1")

    async def test_an_update_after_one_recorded_late_waits_for_its_check(self):
        """C-11: settling an earlier update's flag records it marked to be checked; the next update must not record
        over that mark (a restamp at the same version would clear it without any check)."""
        env = self.env
        await self.create()
        update = env.registry.update

        def full_disk(name, **fields):
            if "stamp_version" in fields and fields.get("updating", 1) is None:
                raise RegistryError("the manager's registry cannot be written: No space left on device")
            return update(name, **fields)

        with mock.patch.object(env.registry, "update", side_effect=full_disk):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertIn("warning", job["result"])
        env.stub.releases.append("0.25.3")
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.3"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("is marked as not checked", job["error"])
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.25.1")
        self.assertTrue(env.registry.get("garage")["tampered"]["pending"])
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))  # Check again
        self.assertEqual(job["state"], "succeeded", job)
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.3"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.registry.get("garage")["version"], "0.25.3")

    async def test_a_failed_update_clears_its_flag(self):
        env = self.env
        await self.create()
        env.stub.fail[("POST", "/store/addons/local_hri_garage/update")] = "pull failed"
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(env.registry.get("garage")["updating"])
        self.assertEqual(children.cleanup_stale(env.local_apps, env.registry), [])

    async def test_a_delete_of_an_instance_that_is_gone_says_delete(self):
        env = self.env
        await self.create()
        del env.stub.installed["local_hri_garage"]
        shutil.rmtree(self.folder("garage"))
        await env.sv.reload_store()
        job = await env.job(await env.send("DELETE", "/api/instances/garage", {"remove_data": False, "confirm": "garage"}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("local_hri_garage is not installed: nothing to delete", job["error"])
        self.assertNotIn("repair", job["error"])

    async def test_the_store_wait_is_bounded_by_the_clock(self):
        """Each store answer may take up to its own timeout: the wait is measured, not counted in sleeps."""
        env = self.env
        env.manager.store_timeout = 0.4

        async def slow(slug):
            await asyncio.sleep(0.2)
            return None

        start = time.monotonic()
        with mock.patch.object(env.sv, "store_app", side_effect=slow), self.assertRaises(JobFailed):
            await env.manager._wait_store(Job("garage", "create", "t"), "local_hri_garage", "0.25.0")
        self.assertLess(time.monotonic() - start, 1.5)

    async def test_the_request_handlers_read_the_marker_and_registry_off_the_event_loop(self):
        """The checks a request makes before its job starts (marker, registry) read files: never on the event loop."""
        env = self.env
        await self.create()
        on_loop = []
        load, get = children.load_managed, env.registry.get

        def recording(real):
            def call(*args, **kw):
                on_loop.append((real.__name__, threading.current_thread() is threading.main_thread()))
                return real(*args, **kw)
            return call

        def no_run(instance, action, user, work):  # the job's own reads are not the handler's
            return Job(instance, action, user)

        with mock.patch.object(children, "load_managed", recording(load)), \
                mock.patch.object(env.registry, "get", recording(get)), \
                mock.patch.object(env.manager.jobs, "start", side_effect=no_run):
            for path, body in (("garage/stop", {}), ("garage/update", {"version": "0.25.1"}), ("garage/finish", {}),
                               ("garage/repair", {}), ("garage/forget", {"confirm": "garage"})):
                await env.send("POST", f"/api/instances/{path}", body)
            await env.send("DELETE", "/api/instances/garage", {"remove_data": False, "confirm": "garage"})
        self.assertTrue(on_loop)
        self.assertEqual([name for name, main in on_loop if main], [])

    async def test_an_update_onto_hri_0_25_2_s_template(self):
        """HRI 0.25.2 adds backup_pre / backup_post and keeps its own backups: an instance updates onto it."""
        env = self.env
        self.assertEqual((await self.create())["state"], "succeeded")
        env.stub.hri_fixture = FIXTURE_0252
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        config = self.config("garage")
        self.assertIn("ha-backup-running", config["backup_pre"])
        self.assertIn("*_hri_garage/backups/.*.tmp", config["backup_exclude"])
        with open(os.path.join(self.copy_dir("garage"), "config.yaml"), encoding="utf-8") as fh:
            self.assertEqual(yaml.safe_load(fh), config)  # the copy in /data is kept, and checked
        job = await self.create("attic", version="0.25.1")
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIn("backup_post", self.config("attic"))

    async def test_a_git_definition_without_its_app_is_not_installed_from_the_folder(self):
        env = self.env
        job = await self.create("lab", channel="git", ref_kind="branch", ref="main")
        self.assertEqual(job["state"], "succeeded", job)
        del env.stub.installed["local_hri_lab"]  # a definition restored without its app
        with open(os.path.join(self.folder("lab"), "Dockerfile"), "ab") as fh:
            fh.write(b"RUN echo planted\n")  # the tree on disk is anyone's who can write the folder
        status, body = await env.send("POST", "/api/instances/lab/install")
        self.assertEqual(status, 400)
        self.assertIn("never from the tree left in the local apps folder", body["error"])
        self.assertNotIn("local_hri_lab", env.stub.installed)
        _, data = await env.get("/api/instances")
        (row,) = [i for i in data["instances"] if i["name"] == "lab"]
        self.assertEqual(row["actions"], ["delete"])
        self.assertIn("create it again from its branch or tag", row["problem"])

    async def test_finish_setup_and_install(self):
        env = self.env
        await self.create()
        env.registry.update("garage", setup_complete=False)
        env.stub.installed["local_hri_garage"].update(boot="manual", watchdog=False, ingress_panel=False, state="stopped")
        self.assertEqual(env.manager.pending_setup(), ["garage"])
        _, data = await env.get("/api/instances")
        inst = data["instances"][0]
        self.assertIn("finish", inst["actions"])
        self.assertIn("setup", inst["problem"])
        job = await env.job(await env.send("POST", "/api/instances/garage/finish"))
        self.assertEqual(job["state"], "succeeded", job)
        app = env.stub.installed["local_hri_garage"]
        self.assertEqual((app["boot"], app["watchdog"], app["ingress_panel"], app["state"]), ("auto", True, True, "started"))
        self.assertTrue(env.registry.get("garage")["setup_complete"])
        self.assertEqual(env.manager.pending_setup(), [])
        # a definition restored without its app
        del env.stub.installed["local_hri_garage"]
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"][0]["actions"], ["install", "delete"])
        job = await env.job(await env.send("POST", "/api/instances/garage/install"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.installed["local_hri_garage"]["state"], "started")
        status, _ = await env.send("POST", "/api/instances/garage/install")  # installed now: nothing to install
        job = await env.job((status, _))
        self.assertEqual(job["state"], "failed")

    async def test_a_release_beyond_the_first_page_of_the_list(self):
        """The release list is paged (100 a page): a release is checked by its own tag, not found in the first page."""
        env = self.env
        env.stub.page_size = 1
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        for version in ("0.99.0", "9.9.9"):  # none, and a draft
            job = await self.create("attic", version=version)
            self.assertEqual(job["state"], "failed")
            self.assertIn("not a published release", job["error"])

    async def test_a_store_timeout_names_a_file_dated_in_the_future(self):
        """The Supervisor notices a change in the local apps folder by its newest date: one file dated in the future
        (another app's, a restored one) hides every later change."""
        env = self.env
        env.stub.store_frozen = True
        env.manager.store_timeout = 0.2
        job = await self.create()
        self.assertEqual(job["state"], "failed")
        self.assertIn("did not show", job["error"])
        self.assertNotIn("future", job["error"])
        other = os.path.join(env.local_apps, "someones_app")
        os.makedirs(other)
        path = os.path.join(other, "run.sh")
        with open(path, "w") as fh:
            fh.write("x")
        future = time.time() + 400 * 86400
        os.utime(path, (future, future))
        job = await self.create()
        self.assertEqual(job["state"], "failed")
        self.assertIn("someones_app/run.sh", job["error"])
        self.assertIn("in the future", job["error"])

    async def test_a_failed_download_writes_nothing(self):
        job = await self.create(version="0.99.0")
        self.assertEqual(job["state"], "failed")
        job = await self.create("lab", channel="git", ref_kind="branch", ref="no-such-branch")
        self.assertEqual(job["state"], "failed")
        self.assertIn("has no branch", job["error"])
        self.assertEqual(os.listdir(self.env.local_apps), [])
        self.assertEqual(self.env.changing_calls(), [])

    async def test_no_downgrade_below_the_installed_version(self):
        """An update the manager stopped waiting for can leave the app newer than its definition: the guard compares
        against the newer of the two."""
        env = self.env
        await self.create()
        env.stub.installed["local_hri_garage"]["version"] = "0.25.1"
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.0"}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("does not downgrade", job["error"])
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.25.1")
        self.assertEqual(env.changing_calls("local_hri_garage/update"), [])
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)  # the definition catches up, nothing to install
        self.assertEqual((self.marker("garage")["version"], env.stub.installed["local_hri_garage"]["version"]), ("0.25.1", "0.25.1"))

    async def test_a_new_stamping_is_written_at_the_same_version(self):
        """A manager whose stamping changed rewrites an instance's definition even at the same HRI version (the
        Supervisor applies it at the instance's next version change: a same-version update is refused)."""
        env = self.env
        await self.create()
        self.assertEqual(self.marker("garage")["stamp_version"], stamp.STAMP_VERSION)
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.0"}))
        self.assertEqual((job["state"], job["result"].get("unchanged")), ("succeeded", True))
        with mock.patch.object(stamp, "STAMP_VERSION", stamp.STAMP_VERSION + 1):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.0"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(job["result"].get("restamped"))
        self.assertEqual(self.marker("garage")["stamp_version"], stamp.STAMP_VERSION + 1)
        self.assertEqual(env.registry.get("garage")["stamp_version"], stamp.STAMP_VERSION + 1)
        self.assertEqual(env.changing_calls("local_hri_garage/update"), [])  # nothing for the Supervisor to install
        self.assertTrue(any("next version change" in l["msg"] for l in job["lines"]), job["lines"])
        # R2-4: the Supervisor's own Rebuild applies a definition at the same version, unchecked: the note says so
        self.assertTrue(any("The Supervisor's own Rebuild would apply it at once" in l["msg"] for l in job["lines"]))

    async def test_a_moved_release_tag_is_refused_and_flagged(self):
        env = self.env
        await self.create()
        installed_sha = self.marker("garage")["sha"]
        env.stub.moved["v0.25.0"] = sha_of("someone force-pushed the tag")
        with mock.patch.object(stamp, "STAMP_VERSION", stamp.STAMP_VERSION + 1):
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.0"}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("tag moved", job["error"])
        self.assertEqual(self.marker("garage")["sha"], installed_sha)  # the definition is untouched
        _, data = await env.get("/api/instances")
        self.assertIn("tag moved", data["instances"][0]["problem"])
        # a second problem is added to the row, never written over the first
        del env.stub.installed["local_hri_garage"]
        _, data = await env.get("/api/instances")
        problem = data["instances"][0]["problem"]
        self.assertIn("tag moved", problem)
        self.assertIn("defined, but not installed", problem)
        env.stub.installed["local_hri_garage"] = {"slug": "local_hri_garage", "name": "HRI Garage", "version": "0.25.0",
                                                  "state": "started", "url": "https://github.com/trailro/hass-remote-integration",
                                                  "repository": "local"}
        # the same for a repair from GitHub (without the manager's copy of the definition, which needs no download)
        shutil.rmtree(self.folder("garage"))
        shutil.rmtree(self.copy_dir("garage"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed")
        self.assertIn("tag moved", job["error"])
        self.assertFalse(os.path.exists(self.folder("garage")))

    async def test_a_newer_release_is_one_newer_than_the_installed_app_too(self):
        """An update the manager stopped waiting for can leave the app newer than its definition."""
        env = self.env
        await self.create()
        await env.get("/api/releases")
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"][0]["newer_release"], "0.25.1")
        env.stub.installed["local_hri_garage"]["version"] = "0.25.1"  # the definition still says 0.25.0
        _, data = await env.get("/api/instances")
        self.assertIsNone(data["instances"][0]["newer_release"])

    async def test_a_failed_update_puts_the_previous_definition_back(self):
        env = self.env
        await self.create()
        env.stub.fail[("POST", "/store/addons/local_hri_garage/update")] = "Can't pull the image"
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.config("garage")["version"], "0.25.0")
        self.assertEqual(self.marker("garage")["version"], "0.25.0")
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.25.0")
        self.assertEqual(env.stub.store["local_hri_garage"]["version"], "0.25.0")
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])

    async def test_repair_after_a_partial_restore(self):
        env = self.env
        await self.create()
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        before = self.marker("garage")
        shutil.rmtree(self.folder("garage"))
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        (inst,) = data["instances"]
        self.assertEqual((inst["managed"], inst["actions"]), (False, ["repair"]))
        status, _ = await env.send("POST", "/api/instances/garage/start")  # without its folder it is not managed
        self.assertEqual(status, 400)
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        m = self.marker("garage")
        self.assertEqual(m["version"], "0.25.1")
        self.assertIn("repaired_at", m)
        # the marker keeps its history (from the registry: the folder is gone), and the repair is added to it
        self.assertEqual((m["created_at"], m["created_by"], m["updated_by"]),
                         (before["created_at"], "alice", "alice"))
        self.assertEqual(m["history"][:-1], before["history"])
        self.assertEqual(m["history"][-1], {"channel": "release", "version": "0.25.1", "ref_kind": "tag", "ref": "v0.25.1",
                                            "sha": before["sha"], "updated_at": before["updated_at"],
                                            "event": f"repaired (manual) at {m['repaired_at']}", "by": "alice"})
        _, data = await env.get("/api/instances")
        self.assertTrue(data["instances"][0]["managed"])

    async def test_a_copy_of_each_definition_is_kept_in_data(self):
        """The manager's /data (in its own backups) keeps each definition: a release's whole, a git instance's
        config only, never its source tree."""
        env = self.env
        await self.create()
        copy = self.read_tree(self.copy_dir("garage"))
        self.assertEqual(sorted(copy), ["CHANGELOG.md", "DOCS.md", "config.yaml", "copy.json", "translations/en.yaml"])
        folder = self.read_tree(self.folder("garage"))
        for rel in ("config.yaml", "DOCS.md", "CHANGELOG.md", "translations/en.yaml"):
            self.assertEqual(copy[rel], folder[rel], rel)
        meta, m = json.loads(copy["copy.json"]), self.marker("garage")
        self.assertEqual({k: meta[k] for k in ("instance_id", "channel", "version", "sha", "stamp_version")},
                         {k: m[k] for k in ("instance_id", "channel", "version", "sha", "stamp_version")})
        await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(yaml.safe_load(self.read_tree(self.copy_dir("garage"))["config.yaml"])["version"], "0.25.1")
        self.assertEqual(sorted(os.listdir(os.path.join(env.data, "definitions"))), ["garage"])  # no leftovers

        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        copy = self.read_tree(self.copy_dir("lab"))
        self.assertEqual(sorted(copy), ["config.yaml", "copy.json"])
        self.assertEqual(copy["config.yaml"], self.read_tree(self.folder("lab"))["config.yaml"])
        self.assertEqual(json.loads(copy["copy.json"])["sha"], env.stub.refs["main"])
        self.assertLess(sum(len(d) for d in copy.values()), 8192)

        for name in ("garage", "lab"):
            await env.job(await env.send("DELETE", f"/api/instances/{name}", {"remove_data": False, "confirm": name}))
            self.assertFalse(os.path.exists(self.copy_dir(name)))
        env.stub.fail[("POST", "/addons/local_hri_attic/start")] = "Can't start"
        await self.create("attic")
        self.assertFalse(os.path.exists(self.copy_dir("attic")))  # rolled back with the rest

    async def test_repair_of_a_release_from_the_copy_downloads_nothing(self):
        env = self.env
        await self.create()
        before, m = self.read_tree(self.folder("garage")), self.marker("garage")
        shutil.rmtree(self.folder("garage"))
        await env.sv.reload_store()
        paths = list(env.stub.codeload_paths)
        with mock.patch.object(env.gh, "_get", side_effect=AssertionError("GitHub was asked")):
            job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(any("nothing downloaded" in l["msg"] for l in job["lines"]), job["lines"])
        self.assertEqual(env.stub.codeload_paths, paths)
        after = self.read_tree(self.folder("garage"))
        for rel in ("config.yaml", "DOCS.md", "CHANGELOG.md", "translations/en.yaml"):
            self.assertEqual(after[rel], before[rel], rel)
        self.assertEqual({k: self.marker("garage")[k] for k in ("instance_id", "version", "sha", "stamp_version")},
                         {k: m[k] for k in ("instance_id", "version", "sha", "stamp_version")})
        status, _ = await env.send("POST", "/api/instances/garage/restart")
        self.assertEqual(status, 202)

    async def test_repair_of_a_git_instance_from_the_copy_keeps_its_commit(self):
        """The branch moved on since: the definition is written for the installed commit, with its source."""
        env = self.env
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        sha, version = env.stub.refs["main"], env.stub.installed["local_hri_lab"]["version"]
        env.stub.refs["main"] = sha_of("main-moved-on")
        shutil.rmtree(self.folder("lab"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/lab/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.codeload_paths[-1], sha)
        m = self.marker("lab")
        self.assertEqual((m["sha"], m["version"], m["ref"]), (sha, version, "main"))
        self.assertEqual(env.stub.store["local_hri_lab"]["version"], version)  # nothing to rebuild
        with open(os.path.join(self.folder("lab"), "Dockerfile"), encoding="utf-8") as fh:
            self.assertIn(f"ARG HRI_BUILD={sha}", fh.read())
        # the commit cannot be downloaded: nothing is written (never the branch's head: a definition of another version
        # would be offered as an update, and installed on its own with auto_update); the user chooses
        shutil.rmtree(self.folder("lab"))
        await env.sv.reload_store()
        env.stub.commits.discard(sha)
        env.stub.codeload_paths.clear()
        job = await env.job(await env.send("POST", "/api/instances/lab/repair"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("cannot be downloaded", job["error"])
        self.assertFalse(os.path.lexists(self.folder("lab")))
        self.assertNotIn("local_hri_lab", env.stub.store)
        self.assertNotIn("refs/heads/main", env.stub.codeload_paths)
        _, data = await env.get("/api/instances")
        row = data["instances"][0]
        self.assertTrue(row["needs_attention"])
        self.assertEqual(row["actions"], ["update", "delete", "repair"])
        self.assertIn("Rebuild", row["problem"])
        self.assertEqual(row["channel"], "git")
        # Rebuild, asked for: the branch's head, written and installed in one job
        job = await env.job(await env.send("POST", "/api/instances/lab/update", {}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.installed["local_hri_lab"]["version"], f"0.0.0-{sha_of('main-moved-on')[:12]}")
        self.assertEqual(self.marker("lab")["sha"], sha_of("main-moved-on"))
        self.assertNotIn("needs_attention", env.registry.get("lab"))

    async def test_rebuild_onto_the_installed_commit_writes_its_definition(self):
        """A restore brought back an app built from another commit than the registry's (the registry says tag v0.25.1
        at its commit, the installed app is the head of branch main): Repair refuses (not the recorded commit).
        Rebuild onto main, whose head IS the installed commit, writes the definition of that commit, installs nothing,
        and records main at that commit; the commit is checked to be on HRI's main first."""
        env = self.env
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        head, version = env.stub.refs["main"], env.stub.installed["local_hri_lab"]["version"]
        recorded = sha_of("tag-0.25.1")
        env.registry.update("lab", ref_kind="tag", ref="v0.25.1", sha=recorded, version=f"0.0.0-{recorded[:12]}")
        await self._detach("lab")
        shutil.rmtree(self.copy_dir("lab"))
        await self.assert_needs_attention("lab", "no record of the commit of the installed")
        # the same Rebuild of an instance that does not need attention is Repair's, as before
        attention = env.registry.get("lab")["needs_attention"]
        env.registry.update("lab", needs_attention=None)
        job = await env.job(await env.send("POST", "/api/instances/lab/update", {"ref_kind": "branch", "ref": "main"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("Repair writes its definition", job["error"])
        env.registry.update("lab", needs_attention=attention)
        # a commit HRI's compare does not put on the ref: nothing written
        from hrimgr.github import NotHRICommit
        with mock.patch.object(env.gh, "commit_on_ref", side_effect=NotHRICommit(f"commit {head[:12]} is not on it")):
            job = await env.job(await env.send("POST", "/api/instances/lab/update", {"ref_kind": "branch", "ref": "main"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("not a commit of", job["error"])
        self.assertFalse(os.path.lexists(self.folder("lab")))
        self.assertEqual(env.registry.get("lab")["sha"], recorded)
        env.stub.calls.clear()
        with mock.patch.object(env.gh, "commit_on_ref", wraps=env.gh.commit_on_ref) as on_ref:
            job = await env.job(await env.send("POST", "/api/instances/lab/update", {"ref_kind": "branch", "ref": "main"}))
        self.assertEqual(job["state"], "succeeded", job)
        on_ref.assert_awaited_once_with(head, "branch", "main")
        self.assertEqual(env.changing_calls("local_hri_lab"), [])  # the installed version: nothing to update
        self.assertEqual(env.stub.installed["local_hri_lab"]["version"], version)
        self.assertEqual(env.stub.store["local_hri_lab"]["version"], version)
        m = self.marker("lab")
        self.assertEqual((m["sha"], m["version"], m["ref_kind"], m["ref"]), (head, version, "branch", "main"))
        self.assertEqual({k: m["history"][-1][k] for k in ("ref", "sha", "event")},
                         {"ref": "v0.25.1", "sha": recorded, "event": f"rebuilt at the installed commit (manual) at {m['repaired_at']}"})
        with open(os.path.join(self.folder("lab"), "Dockerfile"), encoding="utf-8") as fh:
            self.assertIn(f"ARG HRI_BUILD={head}", fh.read())
        entry = env.registry.get("lab")
        self.assertEqual((entry["sha"], entry["version"], entry["ref_kind"], entry["ref"]), (head, version, "branch", "main"))
        self.assertNotIn("needs_attention", entry)
        with open(os.path.join(self.copy_dir("lab"), "copy.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual((meta["sha"], meta["ref"]), (head, "main"))
        _, data = await env.get("/api/instances")
        row = data["instances"][0]
        self.assertTrue(row["managed"])
        self.assertNotIn("needs_attention", row)
        self.assertIn("update", row["actions"])

    async def test_a_copy_that_does_not_fit_is_not_used(self):
        """Another instance's copy, another version's, or a config the manager would not write: GitHub instead."""
        env = self.env
        await self.create()

        def spoil_meta(**over):
            path = os.path.join(self.copy_dir("garage"), "copy.json")
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({**meta, **over}, fh)

        def spoil_config():
            path = os.path.join(self.copy_dir("garage"), "config.yaml")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("privileged:\n  - SYS_ADMIN\n")

        for label, spoil in (("another instance", lambda: spoil_meta(instance_id="f" * 32)),
                             ("another version", lambda: spoil_meta(version="0.25.1")),
                             ("a privileged config", spoil_config)):
            with self.subTest(case=label):
                shutil.rmtree(self.folder("garage"))
                await env.sv.reload_store()
                spoil()
                env.stub.codeload_paths.clear()
                job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
                self.assertEqual(job["state"], "succeeded", job)
                self.assertTrue(any("copy of the definition is not used" in l["msg"] for l in job["lines"]), job["lines"])
                self.assertEqual(env.stub.codeload_paths, ["refs/tags/v0.25.0"])
                self.assertNotIn("privileged", self.config("garage"))

    async def test_a_repair_whose_build_fails_puts_the_registry_entry_back(self):
        env = self.env
        await self.create()
        env.registry.update("garage", updated_at="2020-01-01T00:00:00+00:00", stamp_version=0)
        before = env.registry.get("garage")
        shutil.rmtree(self.folder("garage"))
        await env.sv.reload_store()
        with mock.patch.object(stamp, "find_configs", return_value=["config.yaml", "docs/config.yaml"]):
            job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed")
        self.assertIn("more than one app", job["error"])
        self.assertEqual(env.registry.get("garage"), before)
        self.assertFalse(os.path.lexists(self.folder("garage")))

    async def test_the_copy_is_the_build_s_own_bytes_never_the_folder_read_back(self):
        """A writer turns translations/ into a link to another folder (the manager's /data) right after the build:
        nothing of it reaches the manager's copy, which later writes definitions back."""
        env = self.env
        secret_dir = os.path.join(env.data, "elsewhere")
        os.makedirs(secret_dir)
        with open(os.path.join(secret_dir, "options.json"), "w", encoding="utf-8") as fh:
            json.dump({"github_token": "ghp_stolen_token_123"}, fh)
        with open(os.path.join(secret_dir, "en.yaml"), "w", encoding="utf-8") as fh:
            fh.write("configuration: {x: ghp_stolen_token_123}\n")
        write_new = children.write_new

        def then_swap(root, name, build, registry):  # right after the build, before anything copies it
            managed = write_new(root, name, build, registry)
            translations = os.path.join(root, f"hri_{name}", "translations")
            shutil.rmtree(translations)
            os.symlink(secret_dir, translations)
            return managed

        save, kept = copies.save, {}

        def saved(*args, **kw):  # what the copy held when it was stored (the failed create removes it afterwards)
            result = save(*args, **kw)
            kept.update(self.read_tree(self.copy_dir("garage")))
            return result

        with mock.patch.object(children, "write_new", side_effect=then_swap), \
                mock.patch.object(copies, "save", side_effect=saved):
            job = await self.create()
        self.assertEqual(job["state"], "failed", job)  # the folder changed after the write: nothing installed
        self.assertIn("changed after the manager wrote it", job["error"])
        self.assertIn("translations/en.yaml", kept)
        self.assertNotIn("translations/options.json", kept)
        self.assertFalse(any(b"ghp_stolen" in data for data in kept.values()))

    async def test_a_copy_is_used_only_as_stamping_writes_it(self):
        """A copy's config must be what stamping gives (stamping it again changes nothing), its other files UTF-8 text
        of the names a copy holds, within the size caps; the config is written by the manager's own dumper, never as
        the copy's bytes."""
        env = self.env
        await self.create()
        base = self.copy_dir("garage")

        def edit(rel, fn):
            path = os.path.join(base, *rel.split("/"))
            with open(path, "rb") as fh:
                data = fh.read()
            with open(path, "wb") as fh:
                fh.write(fn(data))

        def add(rel, data):
            os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
            with open(os.path.join(base, rel), "wb") as fh:
                fh.write(data)

        cases = {
            "a panel title stamping would not write": lambda: edit("config.yaml", lambda d: d.replace(b"panel_title: HRI garage", b"panel_title: Anything")),
            "a key stamping drops": lambda: edit("config.yaml", lambda d: d + b"webui: http://[HOST]:[PORT:8087]\n"),
            "DOCS.md that is not UTF-8": lambda: edit("DOCS.md", lambda d: d + b"\xff\xfe"),
            "a translation that is an app": lambda: add("translations/config.yaml", b"slug: x\nname: x\nversion: '1'\n"),
            "a translation that is not a mapping": lambda: edit("translations/en.yaml", lambda d: b"- a list\n"),
            "more than a copy may hold": lambda: mock.patch("hrimgr.copies.MAX_TOTAL", 100).start(),
        }
        for label, spoil in cases.items():
            with self.subTest(case=label):
                shutil.rmtree(self.folder("garage"))
                await env.sv.reload_store()
                spoil()
                env.stub.codeload_paths.clear()
                job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
                mock.patch.stopall()
                self.assertEqual(job["state"], "succeeded", job)
                self.assertTrue(any("copy of the definition is not used" in l["msg"] for l in job["lines"]), job["lines"])
                self.assertEqual(env.stub.codeload_paths, ["refs/tags/v0.25.0"])
        # a comment in the copy's config (valid YAML, same mapping) does not reach the definition: it is dumped anew
        shutil.rmtree(self.folder("garage"))
        await env.sv.reload_store()
        edit("config.yaml", lambda d: d + b"# written by someone else\n")
        env.stub.codeload_paths.clear()
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual((job["state"], env.stub.codeload_paths), ("succeeded", []), job)
        with open(os.path.join(self.folder("garage"), "config.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("someone else", text)
        self.assertTrue(text.startswith("# Written by HRI Manager from "))

    async def _detach(self, name):
        shutil.rmtree(self.folder(name))
        await self.env.sv.reload_store()
        self.env.stub.codeload_paths.clear()

    async def assert_needs_attention(self, name, words):
        env = self.env
        job = await env.job(await env.send("POST", f"/api/instances/{name}/repair"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn(words, job["error"])
        self.assertFalse(os.path.lexists(self.folder(name)))
        self.assertNotIn(f"local_hri_{name}", env.stub.store)  # nothing the Supervisor could offer as an update
        self.assertIn(words, env.registry.get(name)["needs_attention"]["reason"])
        _, data = await env.get("/api/instances")
        row = next(i for i in data["instances"] if i["name"] == name)
        self.assertEqual(row["actions"], ["update", "delete", "repair"])
        self.assertIn("needs attention", row["problem"])

    async def test_repair_writes_only_the_installed_version(self):
        """The commit the manager recorded is not the installed version: nothing is written."""
        env = self.env
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        other = sha_of("another commit")
        env.registry.update("lab", sha=other)
        await self._detach("lab")
        shutil.rmtree(self.copy_dir("lab"))
        await self.assert_needs_attention("lab", "no record of the commit of the installed")
        self.assertEqual(env.stub.codeload_paths, [])

    async def test_repair_downloads_a_commit_only_from_hri_s_own_history(self):
        """codeload serves a fork's commits under HRI's name.  A commit from /data (restored or crafted) with the
        installed version's digits is used only when HRI's repository has it on the registry's branch or tag."""
        env = self.env
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        good = env.stub.refs["main"]
        fork = good[:12] + "f" * 28  # the same version, 0.0.0-<first 12 hex>: a fork's commit made to look like it
        env.stub.commits.add(fork)
        env.registry.update("lab", sha=fork)
        path = os.path.join(self.copy_dir("lab"), "copy.json")
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**meta, "sha": fork}, fh)
        await self._detach("lab")
        await self.assert_needs_attention("lab", "not a commit of")
        self.assertNotIn(fork, env.stub.codeload_paths)
        # the branch deleted: the commit cannot be checked either
        env.registry.update("lab", sha=good)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**meta, "sha": good}, fh)
        del env.stub.refs["main"]
        await self.assert_needs_attention("lab", "not a commit of")
        self.assertEqual(env.stub.codeload_paths, [])

    async def test_repair_writes_only_the_installed_channel(self):
        env = self.env
        await self.create()
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        # the registry says release, the installed app is a build of a git version
        env.stub.installed["local_hri_garage"].update(build=True, version="0.0.0-0123456789ab")
        await self._detach("garage")
        await self.assert_needs_attention("garage", "is a git build")
        # the registry says git, the installed app is a release image
        env.stub.installed["local_hri_lab"].update(build=False, version="0.25.1")
        await self._detach("lab")
        await self.assert_needs_attention("lab", "is a release")
        # neither
        env.stub.installed["local_hri_lab"].update(build=True, version="0.25.1")
        await self.assert_needs_attention("lab", "neither an HRI release nor a git build")
        self.assertEqual(env.stub.codeload_paths, [])

    async def test_an_unpublished_release_needs_attention_then_update_or_delete(self):
        env = self.env
        await self.create()
        await self._detach("garage")
        shutil.rmtree(self.copy_dir("garage"))
        env.stub.releases.remove("0.25.0")
        await self.assert_needs_attention("garage", "not a published release")
        for version in ("0.25.0", "0.24.0"):  # the installed version is Repair's; never lower
            job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": version}))
            self.assertEqual(job["state"], "failed", job)
            self.assertFalse(os.path.lexists(self.folder("garage")))
        env.stub.fail[("POST", "/store/addons/local_hri_garage/update")] = "Can't pull"
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "failed", job)
        self.assertFalse(os.path.lexists(self.folder("garage")))  # removed again: back as it was
        self.assertIn("needs_attention", env.registry.get("garage"))
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual((env.stub.installed["local_hri_garage"]["version"], self.marker("garage")["version"]), ("0.25.1", "0.25.1"))
        # Delete of an instance that needs attention: uninstalled, forgotten, nothing left behind
        await self._detach("garage")
        env.stub.releases.remove("0.25.1")
        shutil.rmtree(self.copy_dir("garage"))
        await self.assert_needs_attention("garage", "not a published release")
        status, _ = await env.send("DELETE", "/api/instances/garage", {"remove_data": False})
        self.assertEqual(status, 400)  # the typed name, as always
        job = await env.job(await env.send("DELETE", "/api/instances/garage", {"remove_data": False, "confirm": "garage"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertNotIn("local_hri_garage", env.stub.installed)
        self.assertIsNone(env.registry.get("garage"))
        self.assertEqual(os.listdir(env.local_apps), [])
        self.assertFalse(os.path.exists(self.copy_dir("garage")))

    async def test_an_update_of_an_instance_that_needs_attention_keeps_its_history(self):
        """Update (or Rebuild) of an instance whose definition is gone: the new marker carries who created it and its
        history (from the registry: the folder is gone), with the update added as for any update."""
        env = self.env
        await self.create()
        env.registry.update("garage", created_by="carol")
        before = env.registry.get("garage")
        await self._detach("garage")
        shutil.rmtree(self.copy_dir("garage"))
        env.stub.releases.remove("0.25.0")
        await self.assert_needs_attention("garage", "not a published release")
        job = await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual(job["state"], "succeeded", job)
        m = self.marker("garage")
        self.assertEqual((m["version"], m["created_at"], m["created_by"], m["updated_by"]),
                         ("0.25.1", before["created_at"], "carol", "alice"))
        self.assertEqual(m["history"], [{k: before[k] for k in ("channel", "version", "ref_kind", "ref", "sha", "updated_at")}])
        entry = env.registry.get("garage")
        self.assertEqual((entry["created_by"], entry["updated_by"], entry["history"]), ("carol", "alice", m["history"]))

    async def test_a_template_source_never_writes_a_line_of_its_own(self):
        """copy.json's (or a marker's) template_source becomes the config's header comment: a newline in it would add
        keys the Supervisor reads."""
        env = self.env
        await self.create()
        evil = "https://codeload.github.com/x\nprivileged:\n  - SYS_ADMIN\n#"
        path = os.path.join(self.copy_dir("garage"), "copy.json")
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**meta, "template_source": evil}, fh)
        await self._detach("garage")
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertTrue(any("copy of the definition is not used" in l["msg"] for l in job["lines"]), job["lines"])
        self.assertNotIn("privileged", self.config("garage"))
        # a marker with such a source is not the manager's, and is not copied at the start
        shutil.rmtree(self.copy_dir("garage"))
        self._write_marker({**self.marker("garage"), "template_source": evil})
        await env.manager.auto_repair_loop()
        self.assertFalse(os.path.exists(self.copy_dir("garage")))
        status, data = await env.send("POST", "/api/instances/garage/restart")
        self.assertEqual(status, 400, data)
        for bad in ("x" * 501, "https://example.com/a b", "a\rb"):
            with self.subTest(source=bad):
                self._write_marker({**self.marker("garage"), "template_source": bad})
                status, _ = await env.send("POST", "/api/instances/garage/restart")
                self.assertEqual(status, 400)

    async def test_records_of_an_instance_that_is_gone_are_listed_and_forgotten(self):
        """Uninstalled outside the manager and its folder gone: the registry entry and the copy are listed, with Forget."""
        env = self.env
        await self.create()
        await self.create("attic")
        for name in ("garage", "attic"):
            del env.stub.installed[f"local_hri_{name}"]
            shutil.rmtree(self.folder(name))
        env.registry.remove("attic")  # a copy without its registry entry
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"], [])
        orphans = {o["instance"]: o for o in data["others"] if o["kind"] == "orphan"}
        self.assertEqual(sorted(orphans), ["attic", "garage"])
        for o in orphans.values():
            self.assertEqual((o["actions"], o["installed"]), (["forget"], False))
        for body in ({}, {"confirm": "GARAGE"}):
            status, _ = await env.send("POST", "/api/instances/garage/forget", body)
            self.assertEqual(status, 400)
        for name in ("garage", "attic"):
            job = await env.job(await env.send("POST", f"/api/instances/{name}/forget", {"confirm": name}))
            self.assertEqual(job["state"], "succeeded", job)
            self.assertIsNone(env.registry.get(name))
            self.assertFalse(os.path.exists(self.copy_dir(name)))
        _, data = await env.get("/api/instances")
        self.assertEqual([o for o in data["others"] if o["kind"] == "orphan"], [])
        # never an installed app, and never a name with a folder
        job = await env.job(await env.send("POST", "/api/instances/foreign/forget", {"confirm": "foreign"}))
        self.assertEqual(job["state"], "failed")
        await self.create()
        status, _ = await env.send("POST", "/api/instances/garage/forget", {"confirm": "garage"})
        self.assertEqual(status, 400)
        self.assertIsNotNone(env.registry.get("garage"))
        self.assertEqual(env.changing_calls("foreign"), [])

    def _hand_made_app(self, folder="my_garage", slug="hri_garage"):
        """A local app someone wrote by hand in another folder, with an instance's slug and HRI's url, installed."""
        path = os.path.join(self.env.local_apps, folder)
        os.makedirs(path)
        with open(os.path.join(path, "config.yaml"), "w") as fh:
            fh.write(f"slug: {slug}\nname: My garage\nversion: '0.25.0'\nurl: https://github.com/trailro/hass-remote-integration\n")
        self.env.stub.installed[f"local_{slug}"] = {"slug": f"local_{slug}", "name": "My garage", "version": "0.25.0",
                                                    "state": "started", "url": "https://github.com/trailro/hass-remote-integration",
                                                    "repository": "local"}

    async def test_repair_only_for_a_detached_app_the_store_does_not_define(self):
        """A hand-made local app with an instance's slug and HRI's url is not detached: Repair would write a second
        definition of its slug and make it the manager's (deletable with its data).  Refused even when the registry
        holds that name (an instance of it created once, its folder gone)."""
        env = self.env
        self._hand_made_app()
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"], [])
        self.assertIn("local_hri_garage", [o["slug"] for o in data["others"]])
        status, body = await env.send("POST", "/api/instances/garage/repair")
        self.assertEqual(status, 400, body)  # not in the registry: not even a job
        register(env.registry, marker("garage"))
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed")
        self.assertIn("not detached", job["error"])
        self.assertFalse(os.path.lexists(self.folder("garage")))

    async def test_a_detached_app_the_registry_does_not_hold_is_never_repaired(self):
        """A hand-made local app with an instance's slug and HRI's url, detached (its folder gone): not the manager's.
        Repair would write a marker and a registry entry for it, and it could then be deleted with its data."""
        env = self.env
        self._hand_made_app()
        shutil.rmtree(os.path.join(env.local_apps, "my_garage"))
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"], [])
        (other,) = [o for o in data["others"] if o["slug"] == "local_hri_garage"]
        self.assertEqual(other["kind"], "local")
        self.assertIn("not managed", other["problem"])
        self.assertNotIn("actions", other)
        status, body = await env.send("POST", "/api/instances/garage/repair")
        self.assertEqual(status, 400, body)
        self.assertIn("registry", body["error"])
        self.assertFalse(os.path.lexists(self.folder("garage")))
        self.assertIsNone(env.registry.get("garage"))
        # and the job itself refuses too, whoever starts it
        job = await env.job((202, {"job": env.manager.jobs.start("garage", "repair", "t", lambda j: env.manager._repair(j, "garage", "t")).as_dict()}))
        self.assertEqual(job["state"], "failed")
        self.assertIn("registry", job["error"])
        self.assertFalse(os.path.lexists(self.folder("garage")))
        self.assertEqual(env.changing_calls(), [])

    async def test_repair_refuses_when_the_store_finds_a_definition_after_reloading(self):
        env = self.env
        await self.create()
        shutil.rmtree(self.folder("garage"))
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"][0]["actions"], ["repair"])  # detached as far as the Supervisor knew
        path = os.path.join(env.local_apps, "somewhere")  # meanwhile a definition of the slug appears elsewhere
        os.makedirs(path)
        with open(os.path.join(path, "config.yaml"), "w") as fh:
            fh.write("slug: hri_garage\nname: x\nversion: '0.25.0'\n")
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed")
        self.assertFalse(os.path.lexists(self.folder("garage")))

    async def test_a_name_whose_host_name_is_taken_is_refused(self):
        """The Supervisor names an app's host after its slug with _ as -: local_hri_a-b and local_hri_a_b collide."""
        env = self.env
        env.stub.installed["local_hri_a-b"] = {"slug": "local_hri_a-b", "name": "Hand-made", "version": "1", "state": "started",
                                               "url": "https://example.com", "repository": "local"}
        job = await self.create("a_b")
        self.assertEqual(job["state"], "failed")
        self.assertIn("host name", job["error"])
        self.assertEqual(os.listdir(env.local_apps), [])
        self.assertEqual(env.changing_calls(), [])

    async def test_foreign_apps_are_never_changed(self):
        env = self.env
        for action in ("start", "stop", "restart", "update", "repair"):
            status, body = await env.send("POST", f"/api/instances/foreign/{action}")
            self.assertEqual(status, 400, body)
        status, _ = await env.send("DELETE", "/api/instances/foreign")
        self.assertEqual(status, 400)
        job = await self.create("foreign")
        self.assertEqual(job["state"], "failed")
        self.assertIn("already installed", job["error"])
        self.assertEqual(env.changing_calls(), [])

    async def test_a_local_build_of_hri_is_not_the_published_app(self):
        env = self.env
        env.stub.installed["local_hass_remote_integration"] = {
            "slug": "local_hass_remote_integration", "name": "hass-remote-integration", "version": "0.25.1",
            "state": "started", "url": "https://github.com/trailro/hass-remote-integration", "repository": "local"}
        _, data = await env.get("/api/instances")
        kinds = {o["slug"]: o["kind"] for o in data["others"]}
        self.assertEqual(kinds["5c53de3b_hass_remote_integration"], "published")
        self.assertEqual(kinds["local_hass_remote_integration"], "local_build")
        self.assertEqual(env.changing_calls(), [])

    async def test_the_marker_gates_every_changing_action(self):
        env = self.env
        await self.create()
        env.stub.calls.clear()
        good = self.marker("garage")
        entry = env.registry.get("garage")
        cases = {
            "no marker": lambda: os.unlink(os.path.join(self.folder("garage"), children.MARKER)),
            "another slug": lambda: self._write_marker({**good, "slug": "core_ssh"}),
            "another manager": lambda: self._write_marker({**good, "manager": "x"}),
            "a marker link": lambda: self._link_marker(),
            "no registry entry": lambda: env.registry.remove("garage"),
            "another instance id in the marker": lambda: self._write_marker({**good, "instance_id": "f" * 32}),
        }
        for label, spoil in cases.items():
            self._write_marker(good)
            env.registry.put("garage", entry)
            spoil()
            for method, path, body in (("POST", "/api/instances/garage/start", {}), ("POST", "/api/instances/garage/stop", {}),
                                       ("POST", "/api/instances/garage/restart", {}), ("POST", "/api/instances/garage/update", {}),
                                       ("DELETE", "/api/instances/garage", {}), ("DELETE", "/api/instances/garage", {"remove_data": True, "confirm": "garage"})):
                with self.subTest(case=label, path=path):
                    status, data = await env.send(method, path, body)
                    self.assertEqual(status, 400, data)
                    self.assertIn("not an instance of this manager", data["error"])
        self.assertEqual(env.changing_calls(), [])

    async def test_a_marker_without_the_registry_is_not_managed(self):
        """A marker written by someone else (Samba, SSH, another app) over a user's own app: listed as not managed,
        no action, nothing changed."""
        env = self.env
        os.makedirs(self.folder("garage"))
        with open(os.path.join(self.folder("garage"), "config.yaml"), "w") as fh:
            fh.write("slug: hri_garage\nname: My garage\nversion: '1.0'\nurl: https://github.com/trailro/hass-remote-integration\n")
        self._write_marker(marker("garage"))
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"], [])
        (other,) = [o for o in data["others"] if o["slug"] == "local_hri_garage"]
        self.assertIn("registry has no instance garage", other["problem"])
        for method, path, body in (("POST", "/api/instances/garage/stop", {}), ("POST", "/api/instances/garage/update", {}),
                                   ("DELETE", "/api/instances/garage", {"remove_data": True, "confirm": "garage"})):
            status, _ = await env.send(method, path, body)
            self.assertEqual(status, 400)
        self.assertEqual(env.changing_calls(), [])

    async def test_the_registry_follows_the_instance(self):
        env = self.env
        await self.create()
        entry = env.registry.get("garage")
        m = self.marker("garage")
        self.assertEqual((entry["instance_id"], entry["channel"], entry["version"], entry["sha"]),
                         (m["instance_id"], "release", "0.25.0", m["sha"]))
        await env.job(await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"}))
        self.assertEqual((env.registry.get("garage")["version"], env.registry.get("garage")["instance_id"]), ("0.25.1", m["instance_id"]))
        await env.job(await env.send("DELETE", "/api/instances/garage", {"remove_data": False, "confirm": "garage"}))
        self.assertIsNone(env.registry.get("garage"))
        env.stub.fail[("POST", "/addons/local_hri_attic/start")] = "Can't start"
        job = await self.create("attic")
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(env.registry.get("attic"))  # rolled back with the rest

    async def test_repair_of_a_git_instance_from_the_registry(self):
        """The folder is gone (a full backup on current Supervisors skips the local apps folder): the registry, in the
        manager's own /data, still knows the branch."""
        env = self.env
        await self.create("lab", channel="git", ref_kind="branch", ref="main")
        instance_id = self.marker("lab")["instance_id"]
        shutil.rmtree(self.folder("lab"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/lab/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        m = self.marker("lab")
        self.assertEqual((m["channel"], m["ref_kind"], m["ref"], m["instance_id"]), ("git", "branch", "main", instance_id))
        self.assertEqual(env.stub.store["local_hri_lab"]["version"], env.stub.installed["local_hri_lab"]["version"])
        status, _ = await env.send("POST", "/api/instances/lab/restart")
        self.assertEqual(status, 202)

    def _write_marker(self, data):
        path = os.path.join(self.folder("garage"), children.MARKER)
        if os.path.lexists(path):
            os.unlink(path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _link_marker(self):
        path = os.path.join(self.folder("garage"), children.MARKER)
        elsewhere = os.path.join(self.env.data, "m.json")
        os.replace(path, elsewhere)
        os.symlink(elsewhere, path)

    async def test_validation_before_any_job(self):
        env = self.env
        for body, word in (({"name": "Bad"}, "lowercase"), ({"name": "manager", "version": "0.25.0"}, "reserved"),
                           ({"name": "x", "version": "0.24.0"}, "0.25.0"), ({"name": "x", "channel": "nightly"}, "channel"),
                           ({"name": "x", "channel": "git", "ref": "../etc"}, "ref"), ({"name": "x", "version": ["0.25.0"]}, "release")):
            with self.subTest(body=body):
                status, data = await env.send("POST", "/api/instances", {"channel": "release", **body})
                self.assertEqual(status, 400)
                self.assertIn(word, data["error"])
        await self.create()
        status, data = await env.send("DELETE", "/api/instances/garage", {"remove_data": True, "confirm": "wrong"})
        self.assertEqual(status, 400)
        status, data = await env.send("DELETE", "/api/instances/garage", {"remove_data": "yes", "confirm": "garage"})
        self.assertEqual(status, 400)
        # every delete needs the typed name, with the data or without: the uninstall drops the instance's options
        # (its password and ingress_users) either way
        for body in ({"remove_data": False}, {}, {"remove_data": False, "confirm": "GARAGE"}, {"confirm": ["garage"]}):
            with self.subTest(body=body):
                status, data = await env.send("DELETE", "/api/instances/garage", body)
                self.assertEqual(status, 400, data)
                self.assertIn("typed", data["error"])
        self.assertIn("local_hri_garage", env.stub.installed)
        self.assertEqual(env.changing_calls("local_hri_garage/uninstall"), [])

    async def test_one_job_per_instance(self):
        env = self.env
        env.stub.delay = 0.3
        first = await env.send("POST", "/api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"})
        self.assertEqual(first[0], 202)
        status, data = await env.send("POST", "/api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"})
        self.assertEqual(status, 409)
        self.assertIn("busy", data["error"])
        _, listing = await env.get("/api/instances")
        await env.job(first)

    async def test_no_secret_leaves_the_manager(self):
        env = self.env
        await self.create()
        for path in ("/api/instances", "/api/status", "/api/jobs"):
            resp = await env.client.get(path)
            text = await resp.text()
            with self.subTest(path=path):
                self.assertEqual(resp.status, 200)
                self.assertNotIn("child-secret-password", text)
                self.assertNotIn("never-shown", text)
                self.assertNotIn("test-token", text)

    async def test_status(self):
        status, data = await self.env.get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual((data["version"], data["supervisor"], data["homeassistant"], data["role"]), (VERSION, "2026.09.3", "2026.9.3", "manager"))
        self.assertTrue(data["role_ok"] and data["map_ok"] and data["dev"])
        self.assertEqual(data["problems"], [])


if __name__ == "__main__":
    unittest.main()
