"""Entry point: ``python -m hrimgr`` in the app's container."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiohttp import web

from . import VERSION, children
from .corews import CoreUsers
from .github import GitHub
from .instances import Manager
from .jobs import Jobs
from .registry import FILE_NAME, Registry
from .settings import LOG_FORMAT, Redact, RedactingFormatter, SettingsError, from_environment
from .supervisor import SupervisorClient
from .web import create_app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
    try:
        settings = from_environment()
    except SettingsError as err:
        logging.getLogger("hrimgr").error("not started: %s", err)
        return 1
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(Redact(settings.secrets))
        handler.setFormatter(RedactingFormatter(settings.secrets))
    if settings.debug:
        root.setLevel(logging.DEBUG)
    log = logging.getLogger("hrimgr")
    if settings.dev:
        log.warning("DEVELOPMENT MODE: peers %s, Supervisor %s", ", ".join(sorted(settings.peers)), settings.supervisor_url)
    for note in children.cleanup_stale(settings.local_apps):
        log.info("local apps folder: %s", note)

    async def build() -> web.Application:
        sv = SupervisorClient(settings.supervisor_url, settings.supervisor_token)
        gh = GitHub(f"{settings.data_dir}/releases.json", settings.github_token, settings.github_api, settings.codeload)
        jobs = Jobs()
        registry = Registry(os.path.join(settings.data_dir, FILE_NAME))
        manager = Manager(settings.local_apps, sv, gh, jobs, registry, dev=settings.dev)
        users = CoreUsers(settings.core_ws_url, settings.supervisor_token)
        app = create_app(settings, manager, users)
        background: list[asyncio.Task] = []

        async def close(_app: web.Application) -> None:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            for job in jobs.recent():
                if job.task and not job.task.done():
                    job.task.cancel()
            await jobs.wait_all()
            await sv.close()
            await gh.close()
            await users.close()

        async def report(_app: web.Application) -> None:
            for name in manager.pending_setup():
                log.warning("instance %s: its create stopped before its setup finished; the page offers Finish setup", name)
            # instances left detached by a restore get their definitions written again, by a loop of its own (the
            # page is served meanwhile, and nobody needs to open it)
            background.append(asyncio.get_running_loop().create_task(manager.auto_repair_loop()))

        app.on_startup.append(report)
        app.on_cleanup.append(close)
        return app

    log.info("HRI Manager %s listening on port %d", VERSION, settings.port)
    web.run_app(build(), host="0.0.0.0", port=settings.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
