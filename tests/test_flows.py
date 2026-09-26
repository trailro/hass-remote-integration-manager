"""Create, update, start/stop/restart, delete and repair through the API, against the fake Supervisor and GitHub over
real HTTP; rollbacks on failure; and the marker and slug checks in front of every changing action."""

import json
import os
import shutil
import unittest

import yaml

from hrimgr import children

from .env import Env
from .fakes.tarballs import sha_of
from .helpers import marker, tmpdir


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

        job = await env.job(await env.send("DELETE", "/api/instances/garage", {"remove_data": False}))
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
        self.assertEqual(env.stub.installed["local_hri_lab"]["version"], f"0.0.0-{sha[:7]}")
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
        self.assertEqual(env.stub.installed["local_hri_lab"]["version"], f"0.0.0-{sha_of('main-2')[:7]}")
        self.assertEqual(self.marker("lab")["history"][0]["sha"], sha)

        job = await env.job(await env.send("DELETE", "/api/instances/lab", {"remove_data": True, "confirm": "lab"}))
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIn(("POST", "/addons/local_hri_lab/uninstall", {"remove_config": True}), env.stub.calls)
        self.assertEqual(set(env.stub.codeload_paths), {"refs/heads/main"})  # always the full ref

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

    async def test_a_failed_download_writes_nothing(self):
        job = await self.create(version="0.99.0")
        self.assertEqual(job["state"], "failed")
        job = await self.create("lab", channel="git", ref_kind="branch", ref="no-such-branch")
        self.assertEqual(job["state"], "failed")
        self.assertIn("has no branch", job["error"])
        self.assertEqual(os.listdir(self.env.local_apps), [])
        self.assertEqual(self.env.changing_calls(), [])

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
        definition of its slug and make it the manager's (deletable with its data)."""
        env = self.env
        self._hand_made_app()
        await env.sv.reload_store()
        _, data = await env.get("/api/instances")
        self.assertEqual(data["instances"], [])
        self.assertIn("local_hri_garage", [o["slug"] for o in data["others"]])
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed")
        self.assertIn("not detached", job["error"])
        self.assertFalse(os.path.lexists(self.folder("garage")))
        self.assertIsNone(env.registry.get("garage"))

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

    async def test_foreign_apps_are_never_changed(self):
        env = self.env
        for action in ("start", "stop", "restart", "update", "repair"):
            status, body = await env.send("POST", f"/api/instances/foreign/{action}")
            if action == "repair":  # a job that looks and refuses
                job = await env.job((status, body))
                self.assertEqual(job["state"], "failed")
                self.assertIn("left alone", job["error"])
            else:
                self.assertEqual(status, 400, body)
        status, _ = await env.send("DELETE", "/api/instances/foreign")
        self.assertEqual(status, 400)
        job = await self.create("foreign")
        self.assertEqual(job["state"], "failed")
        self.assertIn("already installed", job["error"])
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
        status, data = await env.send("DELETE", "/api/instances/garage", {"remove_data": "yes"})
        self.assertEqual(status, 400)
        self.assertIn("local_hri_garage", env.stub.installed)

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
