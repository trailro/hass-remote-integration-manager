"""Create, update, start/stop/restart, delete and repair through the API, against the fake Supervisor and GitHub over
real HTTP; rollbacks on failure; and the marker and slug checks in front of every changing action."""

import asyncio
import json
import os
import shutil
import time
import unittest
from unittest import mock

import yaml

from hrimgr import children, stamp

from .env import Env
from .fakes.tarballs import sha_of
from .helpers import marker, register, tmpdir


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
        env.stub.fail[("GET", "/addons/local_hri_garage/info")] = "Supervisor busy"
        job = await self.create()
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.stub.installed["local_hri_garage"]["state"], "started")
        self.assertTrue(os.path.isdir(self.folder("garage")))
        self.assertTrue(env.registry.get("garage")["setup_complete"])

    async def _cancel_when(self, job_id: str, line: str) -> dict:
        """Cancel a job (as the manager's stop does) once its log shows ``line``; its final state."""
        job = self.env.manager.jobs.get(job_id)
        for _ in range(500):
            if any(line in l["msg"] for l in job.lines):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(f"the job never logged {line!r}: {job.lines}")
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
        await self._cancel_when(body["job"]["id"], "installing")
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

    async def test_an_update_stopped_midway_puts_the_previous_definition_back(self):
        env = self.env
        await self.create()
        env.stub.delay = 0.3
        status, body = await env.send("POST", "/api/instances/garage/update", {"version": "0.25.1"})
        job = await self._cancel_when(body["job"]["id"], "updating")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.config("garage")["version"], "0.25.0")
        self.assertEqual(self.marker("garage")["version"], "0.25.0")
        self.assertEqual(sorted(os.listdir(env.local_apps)), ["hri_garage"])

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
        # the same for a repair from GitHub (without the manager's copy of the definition, which needs no download)
        shutil.rmtree(self.folder("garage"))
        shutil.rmtree(self.copy_dir("garage"))
        await env.sv.reload_store()
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed")
        self.assertIn("tag moved", job["error"])
        self.assertFalse(os.path.exists(self.folder("garage")))

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
        shutil.rmtree(self.folder("garage"))
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        (inst,) = data["instances"]
        self.assertEqual((inst["managed"], inst["actions"]), (False, ["repair"]))
        status, _ = await env.send("POST", "/api/instances/garage/start")  # without its folder it is not managed
        self.assertEqual(status, 400)
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(self.marker("garage")["version"], "0.25.0")
        self.assertIn("repaired_at", self.marker("garage"))
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
        # the commit cannot be downloaded: the branch, as before
        shutil.rmtree(self.folder("lab"))
        await env.sv.reload_store()
        env.stub.commits.discard(sha)
        job = await env.job(await env.send("POST", "/api/instances/lab/repair"))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(self.marker("lab")["sha"], sha_of("main-moved-on"))
        self.assertEqual(env.stub.codeload_paths[-1], "refs/heads/main")

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
        self.assertEqual((data["version"], data["supervisor"], data["homeassistant"], data["role"]), ("0.1.0", "2026.09.3", "2026.9.3", "manager"))
        self.assertTrue(data["role_ok"] and data["map_ok"] and data["dev"])
        self.assertEqual(data["problems"], [])


if __name__ == "__main__":
    unittest.main()
