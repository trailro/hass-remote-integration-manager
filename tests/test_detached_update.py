"""Detached updates retain their target and the administrator's choices after a lost answer or cancellation."""

import asyncio
import os
import shutil
import unittest
from unittest import mock

from hrimgr import children, instances, names
from hrimgr.supervisor import SupervisorError

from .env import Env
from .fakes.tarballs import sha_of
from .helpers import FIXTURE_0252, tmpdir


class DetachedUpdateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()
        self.env.stub.releases = ["0.26.0", "0.26.1"]  # the update is the latest release
        self.env.stub.hri_fixture = FIXTURE_0252

    async def asyncTearDown(self):
        self.env.assert_only_allowed_calls(self)
        await self.env.close()

    def folder(self, name):
        return os.path.join(self.env.local_apps, f"hri_{name}")

    async def detach(self, name):
        shutil.rmtree(self.folder(name))
        await self.env.sv.reload_store()

    async def create_detached(self, name="garage", **choices):
        job = await self.env.job(await self.env.send("POST", "/api/instances", {
            "name": name, "channel": "release", "version": "0.26.0", **choices}))
        self.assertEqual(job["state"], "succeeded", job)
        self.env.stub.installed[names.supervisor_slug(name)]["options"]["label"] = "keep this option"
        await self.detach(name)

    async def update(self, name="garage", **body):
        return await self.env.job(await self.env.send("POST", f"/api/instances/{name}/update", body))

    async def repair(self, name="garage"):
        job = await self.env.job(await self.env.send("POST", f"/api/instances/{name}/repair"))
        self.assertEqual(job["state"], "succeeded", job)

    def assert_preserved(self, name, access, on):
        env, slug = self.env, names.supervisor_slug(name)
        self.assertEqual(env.registry.get(name)["version"], "0.26.1")
        self.assertIs(env.registry.get(name)[access], on)
        key = "host_dbus" if access == "bluetooth" else access
        self.assertIs(env.stub.installed[slug]["definition"][key], on)
        self.assertEqual(env.stub.installed[slug]["options"]["label"], "keep this option")
        self.assertFalse(any(p.endswith(("/install", "/uninstall")) for _, p in env.changing_calls(slug)))

    async def test_completed_latest_update_lost_answer_keeps_each_access_choice_and_allows_repair(self):
        env, update = self.env, self.env.sv.update

        async def applied_then_lost(managed):
            await update(managed)
            raise SupervisorError("update response lost")

        for access in ("bluetooth", "host_network"):
            for on in (True, False):
                name = f"{access.replace('_', '')}{'on' if on else 'off'}"
                with self.subTest(access=access, on=on):
                    await self.create_detached(name, **{access: not on})
                    env.stub.calls.clear()
                    with mock.patch.object(env.sv, "update", side_effect=applied_then_lost):
                        job = await self.update(name, **{access: on})
                    self.assertEqual(job["state"], "failed", job)
                    self.assertIn("The Supervisor has installed 0.26.1 all the same", job["error"])
                    self.assertTrue(env.registry.get(name)["tampered"]["pending"])
                    self.assert_preserved(name, access, on)
                    checked = await env.job(await env.send("POST", f"/api/instances/{name}/finish"))
                    self.assertEqual(checked["state"], "succeeded", checked)
                    self.assertIsNone(env.registry.get(name)["tampered"])
                    # Even another lost local folder is recoverable at the latest version, with no further update.
                    await self.detach(name)
                    await self.repair(name)
                    self.assertIsNone(env.registry.get(name)["tampered"])
                    self.assert_preserved(name, access, on)
                    self.assertEqual(sum(p.endswith("/update") for _, p in env.changing_calls()), 1)

    async def test_unknown_answer_and_late_completion_repair_uses_pending_target_provenance(self):
        env = self.env
        await self.create_detached(bluetooth=False)
        update, app_info, down = env.sv.update, env.sv.app_info, []

        async def applied_then_lost(managed):
            await update(managed)
            down.append(True)
            raise SupervisorError("update response lost")

        async def unreachable(slug):
            if down:
                raise SupervisorError("info response lost")
            return await app_info(slug)

        with mock.patch.object(env.sv, "update", side_effect=applied_then_lost), \
                mock.patch.object(env.sv, "app_info", side_effect=unreachable):
            job = await self.update(bluetooth=True)
        self.assertIn("is not known yet", job["error"])
        entry = env.registry.get("garage")
        self.assertEqual(entry["version"], "0.26.0")
        self.assertIs(entry["bluetooth"], False)
        self.assertEqual(entry["updating"]["fields"]["sha"], sha_of("tag-0.26.1"))
        self.assertIs(entry["updating"]["fields"]["bluetooth"], True)
        restored_marker = {**children.read_marker(self.folder("garage"), "garage"),
                           **{k: entry.get(k) for k in ("version", "sha", "stamp_version", "bluetooth", "host_network")}}
        self.assertIsNone(children.settle_update(env.registry, "garage", entry, restored_marker, "0.26.1"))
        self.assertEqual(env.registry.get("garage"), entry)
        await self.detach("garage")
        env.stub.calls.clear()
        await self.repair()
        self.assert_preserved("garage", "bluetooth", True)
        self.assertEqual(env.changing_calls(), [])

    async def test_old_installed_version_does_not_erase_an_uncertain_request_at_startup_or_repair(self):
        env = self.env
        await self.create_detached(host_network=True)
        with mock.patch.object(env.sv, "update", side_effect=SupervisorError("update response lost")):
            job = await self.update(host_network=False)
        self.assertIn("is not known yet", job["error"])
        self.assertTrue(os.path.isdir(self.folder("garage")))
        pending = env.registry.get("garage")
        for _ in range(2):
            await env.manager.startup()
            self.assertTrue(os.path.isdir(self.folder("garage")))
            self.assertEqual(env.registry.get("garage"), pending)
        job = await self.update(host_network=True)
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("earlier detached update may still finish", job["error"])
        self.assertEqual(env.registry.get("garage"), pending)
        await self.detach("garage")
        # an automatic Repair never gives the pending update up: the row is not repaired by itself
        env.manager.auto_repair_interval = instances.AUTO_REPAIR_INTERVAL
        _, data = await env.get("/api/instances")
        row = next(i for i in data["instances"] if i["name"] == "garage")
        self.assertIsNone(row["job"])
        self.assertEqual(row["actions"], ["update", "repair", "delete"])
        self.assertEqual(row["pending_update"]["version"], "0.26.1")
        env.manager.auto_repair_interval = None
        self.assertEqual(env.registry.get("garage"), pending)
        self.assertIs(env.registry.get("garage")["host_network"], True)
        job = await self.update(host_network=True)
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("earlier detached update may still finish", job["error"])
        self.assertEqual(env.registry.get("garage"), pending)
        env.stub.calls.clear()
        job = await self.update()  # the original call never applied: rebuild and retry its pinned target
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_preserved("garage", "host_network", False)

    async def test_same_target_retry_recovers_noncompletion_and_keeps_pending_after_retry_refusal(self):
        env = self.env
        await self.create_detached(bluetooth=True)
        with mock.patch.object(env.sv, "update", side_effect=SupervisorError("update response lost")):
            await self.update(bluetooth=False)
        await env.manager.startup()
        pending = env.registry.get("garage")
        env.stub.installed["local_hri_garage"]["version"] = "0.27.0"
        job = await self.update()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("not retried or downgraded", job["error"])
        self.assertEqual(env.registry.get("garage"), pending)
        env.stub.installed["local_hri_garage"]["version"] = "0.26.0"
        env.stub.fail[("POST", "/store/addons/local_hri_garage/update")] = "retry refused"
        job = await self.update()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("retry refused", job["error"])
        self.assertEqual(env.registry.get("garage"), pending)
        env.stub.calls.clear()
        job = await self.update()  # default choices come from the pending target, not the old registry entry
        self.assertEqual(job["state"], "succeeded", job)
        self.assert_preserved("garage", "bluetooth", False)

    async def test_git_retry_refuses_changed_source_even_with_the_original_config(self):
        env = self.env
        job = await env.job(await env.send("POST", "/api/instances", {
            "name": "garage", "channel": "git", "ref_kind": "branch", "ref": "main"}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.detach("garage")
        env.stub.refs["next"] = sha_of("next-commit")
        with mock.patch.object(env.sv, "update", side_effect=SupervisorError("update response lost")):
            await self.update(ref_kind="branch", ref="next")
        manifest = children.digest_tree(self.folder("garage"))
        source = next(rel for rel in manifest if rel.endswith(".py"))
        with open(os.path.join(self.folder("garage"), source), "a", encoding="utf-8") as fh:
            fh.write("\n# changed local source\n")
        with mock.patch.object(env.sv, "update", new=mock.AsyncMock()) as update:
            job = await self.update()
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("source files changed", job["error"])
        update.assert_not_called()
        await self.detach("garage")
        env.stub.history["refs/heads/next"] = {sha_of("next-commit")}
        env.stub.refs["next"] = sha_of("next-newer-commit")
        job = await self.update()  # lost folder: fetch the recorded commit, never the moved branch head
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(env.registry.get("garage")["sha"], sha_of("next-commit"))

    async def test_uncertain_git_update_repair_keeps_the_requested_ref_and_exact_commit(self):
        env = self.env
        job = await env.job(await env.send("POST", "/api/instances", {
            "name": "garage", "channel": "git", "ref_kind": "branch", "ref": "main", "bluetooth": True}))
        self.assertEqual(job["state"], "succeeded", job)
        await self.detach("garage")
        env.stub.refs["next"] = sha_of("next-commit")
        update, app_info, down = env.sv.update, env.sv.app_info, []

        async def applied_then_lost(managed):
            await update(managed)
            down.append(True)
            raise SupervisorError("update response lost")

        async def unreachable(slug):
            if down:
                raise SupervisorError("info response lost")
            return await app_info(slug)

        with mock.patch.object(env.sv, "update", side_effect=applied_then_lost), \
                mock.patch.object(env.sv, "app_info", side_effect=unreachable):
            job = await self.update(ref_kind="branch", ref="next", bluetooth=False)
        self.assertIn("is not known yet", job["error"])
        await self.detach("garage")
        env.stub.calls.clear()
        await self.repair()
        entry = env.registry.get("garage")
        self.assertEqual((entry["ref"], entry["sha"], entry["version"]),
                         ("next", sha_of("next-commit"), names.git_version(sha_of("next-commit"))))
        self.assertIs(entry["bluetooth"], False)
        self.assertEqual(env.changing_calls(), [])

    async def test_definite_refusal_restores_the_detached_entry(self):
        env = self.env
        await self.create_detached(bluetooth=True)
        original = env.registry.get("garage")
        env.stub.fail[("POST", "/store/addons/local_hri_garage/update")] = "pull refused"
        job = await self.update(bluetooth=False)
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("pull refused", job["error"])
        self.assertEqual(env.registry.get("garage"), original)
        self.assertFalse(os.path.lexists(self.folder("garage")))
        self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.26.0")

    async def test_cancelled_sent_update_preserves_target_until_startup(self):
        env = self.env
        for completes in (True, False):
            name = "completed" if completes else "notcompleted"
            with self.subTest(completes=completes):
                await self.create_detached(name, host_network=not completes)
                update, entered, never = env.sv.update, asyncio.Event(), asyncio.Event()

                async def awaiting_response(managed):
                    if completes:
                        await update(managed)
                    entered.set()
                    await never.wait()

                with mock.patch.object(env.sv, "update", side_effect=awaiting_response):
                    _, body = await env.send("POST", f"/api/instances/{name}/update", {"host_network": completes})
                    await asyncio.wait_for(entered.wait(), 5)
                    job = env.manager.jobs.get(body["job"]["id"])
                    job.task.cancel()
                    await asyncio.gather(job.task, return_exceptions=True)
                self.assertTrue(os.path.isdir(self.folder(name)))
                self.assertIs(env.registry.get(name)["updating"]["fields"]["host_network"], completes)
                env.stub.calls.clear()
                await env.manager.startup()
                await env.manager.jobs.wait_all()
                if completes:
                    self.assertIsNone(env.registry.get(name)["updating"])
                    self.assert_preserved(name, "host_network", True)
                    self.assertIsNone(env.registry.get(name)["tampered"])
                else:
                    self.assertTrue(os.path.isdir(self.folder(name)))
                    self.assertEqual(env.registry.get(name)["version"], "0.26.0")
                    self.assertIs(env.registry.get(name)["host_network"], True)
                    self.assertIs(env.registry.get(name)["updating"]["fields"]["host_network"], False)

    async def test_cancelled_before_send_rolls_back(self):
        env = self.env
        await self.create_detached()
        original, entered, never = env.registry.get("garage"), asyncio.Event(), asyncio.Event()

        async def before_send(*args):
            entered.set()
            await never.wait()

        with mock.patch.object(env.manager, "_verify_store", side_effect=before_send):
            _, body = await env.send("POST", "/api/instances/garage/update", {"bluetooth": True})
            await asyncio.wait_for(entered.wait(), 5)
            job = env.manager.jobs.get(body["job"]["id"])
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        self.assertEqual(env.registry.get("garage"), original)
        self.assertFalse(os.path.lexists(self.folder("garage")))

    async def test_supervisor_finishes_after_cancellation_and_startup_checks_the_target(self):
        env = self.env
        await self.create_detached(bluetooth=True)
        update, entered, finish, applied = env.stub._store_action, asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def accepted_request(slug, action, store):
            # Stub.store_action captured this definition before it shielded this independent Supervisor task.
            entered.set()
            await finish.wait()
            response = await update(slug, action, store)
            applied.set()
            return response

        with mock.patch.object(env.stub, "_store_action", side_effect=accepted_request):
            _, body = await env.send("POST", "/api/instances/garage/update", {"bluetooth": False})
            await asyncio.wait_for(entered.wait(), 5)
            job = env.manager.jobs.get(body["job"]["id"])
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            self.assertEqual(env.stub.installed["local_hri_garage"]["version"], "0.26.0")
            # The manager restarts while the Supervisor task is still running, before the version changes.
            pending = env.registry.get("garage")
            await env.manager.startup()
            self.assertEqual(env.registry.get("garage"), pending)
            self.assertTrue(os.path.isdir(self.folder("garage")))
            finish.set()
            await asyncio.wait_for(applied.wait(), 5)
        env.stub.calls.clear()
        await env.manager.startup()
        await env.manager.jobs.wait_all()
        self.assert_preserved("garage", "bluetooth", False)
        self.assertIsNone(env.registry.get("garage")["tampered"])

    async def test_kill_before_send_is_settled_at_startup(self):
        """A hard kill after the flag and definition were written, before the update call: nothing the Supervisor was
        asked can finish it, so the next start removes the definition and clears the flag."""
        env = self.env
        for folder_left in (True, False):
            name = "written" if folder_left else "building"
            with self.subTest(folder_left=folder_left):
                await self.create_detached(name, bluetooth=True)
                original = env.registry.get(name)
                entered, never = asyncio.Event(), asyncio.Event()

                async def before_send(*args):
                    entered.set()
                    await never.wait()

                # no undo: what a kill (power, OOM) leaves, unlike a cancellation
                with mock.patch.object(env.manager, "_verify_store", side_effect=before_send), \
                        mock.patch.object(env.manager, "_undo_detached", new=mock.AsyncMock()):
                    _, body = await env.send("POST", f"/api/instances/{name}/update", {"bluetooth": False})
                    await asyncio.wait_for(entered.wait(), 5)
                    job = env.manager.jobs.get(body["job"]["id"])
                    job.task.cancel()
                    await asyncio.gather(job.task, return_exceptions=True)
                self.assertIsNot(env.registry.get(name)["updating"].get("sent"), True)
                if not folder_left:
                    shutil.rmtree(self.folder(name))
                notes = await env.manager.startup()
                self.assertTrue(any("stopped before it was sent" in n for n in notes), notes)
                self.assertFalse(os.path.lexists(self.folder(name)))
                self.assertEqual(env.registry.get(name), {**original, "updating": None})
                self.assertEqual(env.stub.installed[names.supervisor_slug(name)]["version"], "0.26.0")
                await env.sv.reload_store()
                env.stub.calls.clear()
                job = await self.update(name, version="0.26.1", bluetooth=False)  # another update is not refused
                self.assertEqual(job["state"], "succeeded", job)
                self.assert_preserved(name, "bluetooth", False)

    async def test_sent_flag_is_persisted_before_the_update_call(self):
        env, update, seen = self.env, self.env.sv.update, []
        await self.create_detached()

        async def record(managed):
            seen.append(env.registry.get("garage")["updating"].get("sent"))
            await update(managed)

        with mock.patch.object(env.sv, "update", side_effect=record):
            job = await self.update(bluetooth=True)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(seen, [True])

    async def test_sent_update_that_failed_in_the_supervisor_is_given_up_by_repair(self):
        env = self.env
        for detach in (False, True):
            name = "detached" if detach else "defined"
            slug = names.supervisor_slug(name)
            with self.subTest(detach=detach):
                await self.create_detached(name, bluetooth=True)
                original = env.registry.get(name)
                with mock.patch.object(env.sv, "update", side_effect=SupervisorError("update response lost")):
                    job = await self.update(name, bluetooth=False)
                self.assertIn("is not known yet", job["error"])
                self.assertIs(env.registry.get(name)["updating"]["sent"], True)
                await env.manager.startup()  # the Supervisor still has 0.26.0: kept, it may still finish
                self.assertIsInstance(env.registry.get(name)["updating"], dict)
                if detach:
                    await self.detach(name)
                else:
                    _, data = await env.get("/api/instances")
                    row = next(i for i in data["instances"] if i["name"] == name)
                    self.assertEqual(row["pending_update"], {
                        "version": "0.26.1", "ref_kind": "tag", "ref": "v0.26.1", "from_version": "0.26.0",
                        "sent": True, "bluetooth": False, "host_network": False})
                    self.assertIn("waiting to be finished", row["problem"])
                    self.assertIn("repair", row["actions"])
                env.stub.calls.clear()
                await self.repair(name)
                entry = env.registry.get(name)
                self.assertIsNone(entry.get("updating"))
                self.assertEqual((entry["version"], entry["bluetooth"]), ("0.26.0", True))
                self.assertEqual(entry["sha"], original["sha"])
                self.assertEqual(children.read_marker(self.folder(name), name)["version"], "0.26.0")
                self.assertFalse(any(p.endswith("/update") for _, p in env.changing_calls(slug)))
                job = await self.update(name, version="0.26.1", bluetooth=False)
                self.assertEqual(job["state"], "succeeded", job)
                self.assert_preserved(name, "bluetooth", False)

    async def test_repair_does_not_give_up_an_update_whose_target_is_installed(self):
        env = self.env
        await self.create_detached(bluetooth=True)
        with mock.patch.object(env.sv, "update", side_effect=SupervisorError("update response lost")):
            await self.update(bluetooth=False)
        pending = env.registry.get("garage")
        env.stub.installed["local_hri_garage"]["version"] = "0.26.1"  # it finished after the answer was lost
        job = await env.job(await env.send("POST", "/api/instances/garage/repair"))
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("Check again records it", job["error"])
        self.assertEqual(env.registry.get("garage"), pending)
        self.assertTrue(os.path.isdir(self.folder("garage")))
