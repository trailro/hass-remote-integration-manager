"""The app's entry point: what a stop does, in which order, within the Supervisor's stop timeout."""

import asyncio
import logging
import os
import unittest
from unittest import mock

from aiohttp import web

from hrimgr import __main__ as main_module
from hrimgr.jobs import ShuttingDown
from hrimgr.settings import Settings
from hrimgr.web import MANAGER_KEY

from .env import Env
from .helpers import tmpdir


class ShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_are_cancelled_before_open_requests_are_waited_for(self):
        base = tmpdir(self)
        os.makedirs(os.path.join(base, "local_apps"))
        settings = Settings(supervisor_token="t", supervisor_url="http://127.0.0.1:9/sv", local_apps=os.path.join(base, "local_apps"),
                            data_dir=base, peers=frozenset({"127.0.0.1"}), dev=True)
        root = logging.getLogger()
        handlers = list(root.handlers)
        self.addCleanup(lambda: root.handlers.__setitem__(slice(None), handlers))
        run = {}
        with mock.patch.object(main_module, "from_environment", return_value=settings), \
                mock.patch.object(main_module.web, "run_app", side_effect=lambda app, **kw: run.update(app=app, **kw)):
            self.assertEqual(main_module.main(), 0)
        self.assertEqual(run["shutdown_timeout"], main_module.SHUTDOWN_TIMEOUT)
        self.assertLessEqual(main_module.SHUTDOWN_TIMEOUT, 5)
        app = await run["app"]
        stopped = asyncio.Event()

        async def install(job):
            try:
                await asyncio.sleep(3600)
            finally:
                stopped.set()

        runner = web.AppRunner(app, shutdown_timeout=run["shutdown_timeout"])
        await runner.setup()
        job = app[MANAGER_KEY].jobs.start("garage", "create", "alice", install)
        await asyncio.sleep(0)
        order = []
        with mock.patch.object(runner._server, "shutdown", side_effect=lambda timeout: order.append(("drain", job.state))):
            # as run_app stops: on_shutdown, then the wait for open connections, then on_cleanup
            await runner.cleanup()
        self.assertTrue(stopped.is_set())
        self.assertEqual((job.state, job.error), ("failed", "cancelled: the manager stopped"))
        self.assertEqual(order, [("drain", "failed")])  # cancelled before the wait for open requests

    async def test_no_job_starts_once_the_stop_began_and_the_sessions_outlive_the_jobs(self):
        """R2-19: a request already past the guard when the stop begins gets 503 instead of a job nobody would cancel;
        a job that started all the same is waited for before the sessions it uses are closed."""
        base = tmpdir(self)
        os.makedirs(os.path.join(base, "local_apps"))
        settings = Settings(supervisor_token="t", supervisor_url="http://127.0.0.1:9/sv", local_apps=os.path.join(base, "local_apps"),
                            data_dir=base, peers=frozenset({"127.0.0.1"}), dev=True)
        root = logging.getLogger()
        handlers = list(root.handlers)
        self.addCleanup(lambda: root.handlers.__setitem__(slice(None), handlers))
        run = {}
        with mock.patch.object(main_module, "from_environment", return_value=settings), \
                mock.patch.object(main_module.web, "run_app", side_effect=lambda app, **kw: run.update(app=app, **kw)):
            main_module.main()
        app = await run["app"]
        jobs = app[MANAGER_KEY].jobs
        runner = web.AppRunner(app, shutdown_timeout=run["shutdown_timeout"])
        await runner.setup()
        order, refused, late = [], [], []

        async def short(job):
            try:
                await asyncio.sleep(3600)
            finally:
                await asyncio.sleep(0.05)  # its own rollback, say
                order.append("late job ended")

        def drain(timeout):
            try:
                jobs.start("attic", "create", "alice", short)
            except ShuttingDown as err:
                refused.append(str(err))
            jobs._closing = False  # one that slipped past all the same
            late.append(jobs.start("cellar", "create", "alice", short))

        close = main_module.SupervisorClient.close

        async def closing(client):
            order.append("sessions closed")
            await close(client)

        with mock.patch.object(runner._server, "shutdown", side_effect=drain), \
                mock.patch.object(main_module.SupervisorClient, "close", closing):
            await runner.cleanup()
        self.assertEqual(len(refused), 1)
        self.assertIn("stopping", refused[0])
        self.assertEqual(order, ["late job ended", "sessions closed"])  # cancelled, and ended first
        self.assertEqual(late[0].error, "cancelled: the manager stopped")

    async def test_a_request_after_the_stop_began_gets_503(self):
        env = await Env(tmpdir(self)).start()
        self.addAsyncCleanup(env.close)
        env.manager.jobs.close()
        status, body = await env.send("POST", "/api/instances", {"name": "garage", "channel": "release", "version": "0.25.0"})
        self.assertEqual(status, 503, body)
        self.assertIn("stopping", body["error"])


if __name__ == "__main__":
    unittest.main()
