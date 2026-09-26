"""The app's entry point: what a stop does, in which order, within the Supervisor's stop timeout."""

import asyncio
import logging
import os
import unittest
from unittest import mock

from aiohttp import web

from hrimgr import __main__ as main_module
from hrimgr.settings import Settings
from hrimgr.web import MANAGER_KEY

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


if __name__ == "__main__":
    unittest.main()
