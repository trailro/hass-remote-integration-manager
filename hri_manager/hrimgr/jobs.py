"""Background jobs: an install or update can take minutes, so every action runs as a job the page polls.

One job per instance at a time.  A job keeps its log lines (what the page shows), its result and its error; the
last few finished jobs are kept in memory."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)
KEEP_FINISHED = 50
MAX_LINES = 500


class Busy(Exception):
    pass


class ShuttingDown(Busy):
    """The manager is stopping: no job starts any more (a request already past the guard gets 503)."""


class JobFailed(Exception):
    """A failure whose message is for the user as it is."""


class TagMoved(JobFailed):
    """A release tag that no longer names the commit an instance was installed from."""


class Tampered(JobFailed):
    """The Supervisor installed another definition than the one the manager wrote: the app is uninstalled at once."""


class Unverified(JobFailed):
    """The Supervisor no longer reports a field the manager checks: the app is stopped and marked, not uninstalled."""


class NeedsAttention(JobFailed):
    """Repair cannot get the exact source of the installed version: nothing was written, and the instance waits for
    the user (Update or Rebuild, Delete, or Repair again once the cause is gone); automatic repair leaves it alone."""


class Job:
    def __init__(self, instance: str, action: str, user: str):
        self.id = secrets.token_hex(8)
        self.instance = instance
        self.action = action
        self.user = user
        self.state = "running"
        self.lines: list[dict] = []
        self.result: dict = {}
        self.error = ""
        self.started = time.time()
        self.finished: float | None = None
        self.task: asyncio.Task | None = None
        self._to_close: list = []

    def keep(self, resource):
        """``resource`` (with a close()) is closed when the job ends, whatever it ends with; returned as it is."""
        self._to_close.append(resource)
        return resource

    def close_kept(self) -> None:
        for resource in self._to_close:
            try:
                resource.close()
            except Exception:  # noqa: BLE001 - closing never fails a job
                _LOGGER.debug("closing %r failed", resource, exc_info=True)
        self._to_close.clear()

    def log(self, message: str) -> None:
        _LOGGER.info("%s %s: %s", self.action, self.instance, message)
        self.lines.append({"t": round(time.time() - self.started, 1), "msg": message})
        if len(self.lines) > MAX_LINES:
            del self.lines[1:len(self.lines) - MAX_LINES + 1]

    def as_dict(self) -> dict:
        return {"id": self.id, "instance": self.instance, "action": self.action, "user": self.user, "state": self.state,
                "lines": list(self.lines), "result": self.result, "error": self.error,
                "started": self.started, "finished": self.finished}

    def summary(self) -> dict:
        return {"id": self.id, "action": self.action, "state": self.state}


class Jobs:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._running: dict[str, Job] = {}
        self._closing = False

    def close(self) -> None:
        """No job starts from now on (the stop began): one started after the running ones were cancelled would run
        on, uncancelled, while its sessions are closed under it."""
        self._closing = True

    def running_for(self, instance: str) -> Job | None:
        return self._running.get(instance)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)

    def start(self, instance: str, action: str, user: str, work: Callable[[Job], Awaitable[dict | None]]) -> Job:
        if self._closing:
            raise ShuttingDown("HRI Manager is stopping: try again once it has started again")
        if instance in self._running:
            other = self._running[instance]
            raise Busy(f"{instance} is busy: {other.action} started by {other.user or 'someone'} is still running")
        job = Job(instance, action, user)
        self._jobs[job.id] = job
        self._running[instance] = job
        _LOGGER.info("user %r: %s %s (job %s)", user, action, instance, job.id)
        job.task = asyncio.get_running_loop().create_task(self._run(job, work))
        self._trim()
        return job

    async def _run(self, job: Job, work: Callable[[Job], Awaitable[dict | None]]) -> None:
        try:
            job.result = await work(job) or {}
            job.state = "succeeded"
            job.log("done")
        except asyncio.CancelledError:
            job.state = "failed"
            job.error = "cancelled: the manager stopped"
            raise
        except Exception as err:  # noqa: BLE001 - a job reports every failure instead of dying silently
            job.state = "failed"
            job.error = str(err) or err.__class__.__name__
            if not isinstance(err, JobFailed):
                _LOGGER.exception("%s %s failed", job.action, job.instance)
            job.log(f"failed: {job.error}")
        finally:
            job.close_kept()
            job.finished = time.time()
            self._running.pop(job.instance, None)

    def _trim(self) -> None:
        finished = [j for j in self._jobs.values() if j.state != "running"]
        finished.sort(key=lambda j: j.started)
        for job in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
            self._jobs.pop(job.id, None)

    async def wait_all(self) -> None:
        tasks = [j.task for j in self._running.values() if j.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
