"""The manager wired to the fake Supervisor and fake GitHub (tests/fakes/stub.py) over real HTTP on the loopback."""

from __future__ import annotations

import asyncio
import os

from aiohttp.test_utils import TestClient, TestServer

from hrimgr import supervisor
from hrimgr.corews import CoreUsers
from hrimgr.github import GitHub
from hrimgr.instances import Manager
from hrimgr.jobs import Jobs
from hrimgr.registry import FILE_NAME, Registry
from hrimgr.settings import Settings
from hrimgr.supervisor import SupervisorClient
from hrimgr.web import create_app

from .fakes.stub import ALICE_ID, Stub

# what the Supervisor's ingress sets for Home Assistant's administrator alice
USER = {"X-Remote-User-Id": ALICE_ID, "X-Remote-User-Name": "alice"}
HEADERS = {"X-Requested-With": "fetch", **USER}


class Env:
    def __init__(self, base_dir: str, delay: float = 0.0, **settings):
        self.local_apps = os.path.join(base_dir, "local_apps")
        self.data = os.path.join(base_dir, "data")
        os.makedirs(self.local_apps)
        os.makedirs(self.data)
        self.stub = Stub(self.local_apps, token="test-token", delay=delay)
        self.settings_over = settings

    async def start(self) -> "Env":
        self.stub_server = TestServer(self.stub.app())
        await self.stub_server.start_server()
        base = str(self.stub_server.make_url("")).rstrip("/")
        self.settings = Settings(supervisor_token="test-token", supervisor_url=base + "/sv", local_apps=self.local_apps,
                                 data_dir=self.data, peers=frozenset({"127.0.0.1"}), github_api=base + "/gh",
                                 codeload=base + "/cl", dev=True)
        for key, value in self.settings_over.items():
            setattr(self.settings, key, value)
        self.sv = SupervisorClient(self.settings.supervisor_url, "test-token")
        self.gh = GitHub(os.path.join(self.data, "releases.json"), "", self.settings.github_api, self.settings.codeload)
        self.registry = Registry(os.path.join(self.data, FILE_NAME))
        # no automatic repair here: the tests of the manual one would race it (tests/test_auto_repair.py turns it on)
        self.manager = Manager(self.local_apps, self.sv, self.gh, Jobs(), self.registry, dev=True, poll_interval=0.02,
                               store_timeout=2, auto_repair_interval=None)
        self.users = CoreUsers(self.settings.core_ws_url, "test-token")
        self.client = TestClient(TestServer(create_app(self.settings, self.manager, self.users)), headers=USER)
        await self.client.start_server()
        return self

    async def client_with(self, test, headers: dict | None = None, **settings) -> TestClient:
        """Another client of the same manager, with other settings and default headers (none: no user)."""
        for key, value in settings.items():
            setattr(self.settings, key, value)
        client = TestClient(TestServer(create_app(self.settings, self.manager, self.users)), headers=headers)
        await client.start_server()
        test.addAsyncCleanup(client.close)
        return client

    async def close(self) -> None:
        await self.manager.jobs.wait_all()
        await self.client.close()
        await self.users.close()
        await self.sv.close()
        await self.gh.close()
        await self.stub_server.close()

    async def get(self, path: str, **kw):
        resp = await self.client.get(path, **kw)
        return resp.status, await resp.json()

    async def send(self, method: str, path: str, body: dict | None = None):
        resp = await self.client.request(method, path, json=body or {}, headers={"X-Requested-With": "fetch"})
        return resp.status, await resp.json()

    async def job(self, status_body) -> dict:
        status, body = status_body
        assert status == 202, (status, body)
        job_id = body["job"]["id"]
        for _ in range(500):
            _, data = await self.get(f"/api/jobs/{job_id}")
            if data["job"]["state"] != "running":
                return data["job"]
            await asyncio.sleep(0.02)
        raise AssertionError("the job did not finish")

    def changing_calls(self, slug: str | None = None) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.stub.calls if m == "POST" and p != "/store/reload" and (slug is None or slug in p)]

    def assert_only_allowed_calls(self, test) -> None:
        test.assertEqual(self.stub.unexpected, [])
        for method, path, body in self.stub.calls:
            rule = next((r for r in supervisor.RULES if r.method == method and r.pattern.fullmatch(path)), None)
            test.assertIsNotNone(rule, f"{method} {path} is not in the allow-list")
