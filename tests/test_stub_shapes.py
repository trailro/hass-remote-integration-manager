"""The fake Supervisor (tests/fakes/stub.py) answers as a real one does: the same keys, the same value types and the
same meaning, compared with answers captured from Supervisor 2026.09.2 on Home Assistant OS 18.3
(tests/fixtures/supervisor/: tokens, addresses and options scrubbed, long texts trimmed).

A stub that disagreed with the real Supervisor hid a blocker in 0.1.0: the stub's ``GET /store/addons/<slug>`` said
``version`` for the definition's version, where the Supervisor says ``version_latest`` (``version`` is the INSTALLED
version, null before the install), so every create waited for a version that never came.

``app_info_detached.json`` was not captured (making an app detached changes the machine): it is ``app_info.json``
with the values the Supervisor's code gives a detached app (apps/app.py ``is_detached``, ``data_store`` falling back
to the app's own data, ``with_icon`` & co. False; apps/model.py ``long_description`` None)."""

import json
import os
import re
import shutil
import unittest

from aiohttp.test_utils import TestClient, TestServer

from hrimgr import stamp

from .fakes.stub import SUPERVISOR_FIXTURES, Stub, captured
from .helpers import FIXTURE_CONFIG, tmpdir

AUTH = {"Authorization": "Bearer t"}


def shape(value):
    """Keys and value types, one level down into objects: what a client relies on."""
    if isinstance(value, dict):
        return {k: type(v).__name__ for k, v in value.items()}
    return type(value).__name__


class StubShapeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.local_apps = os.path.join(tmpdir(self), "local_apps")
        os.makedirs(self.local_apps)
        self.stub = Stub(self.local_apps, token="t")
        self.client = TestClient(TestServer(self.stub.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def call(self, method, path):
        resp = await self.client.request(method, "/sv" + path, headers=AUTH, json={} if method == "POST" else None)
        return resp.status, await resp.json()

    def define(self, name="garage", channel="release", version="0.25.0"):
        template = stamp.parse_template(FIXTURE_CONFIG.read_bytes())
        folder = os.path.join(self.local_apps, f"hri_{name}")
        os.makedirs(folder)
        with open(os.path.join(folder, "config.yaml"), "wb") as fh:
            fh.write(stamp.dump(stamp.stamp(template, name, version, channel), "test"))
        if channel == "git":  # a git instance holds HRI's whole tree, its README.md among it
            with open(os.path.join(folder, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("# hass-remote-integration\n")
        return folder

    def assert_like(self, fixture, status, answer, pick=None):
        real = captured(fixture)
        self.assertEqual(status, real["status"], fixture)
        real_answer = real["answer"]
        if pick:
            real_answer, answer = pick(real_answer), pick(answer)
        self.assertEqual(set(answer), set(real_answer), f"{fixture}: keys")
        if "data" in real_answer:
            self.assertEqual(shape(answer["data"]), shape(real_answer["data"]), f"{fixture}: keys and types of data")
        else:
            self.assertEqual(shape(answer), shape(real_answer), f"{fixture}: keys and types")

    async def test_the_store_entry_before_and_after_the_install(self):
        self.define()
        await self.call("POST", "/store/reload")
        status, answer = await self.call("GET", "/store/addons/local_hri_garage")
        self.assert_like("store_app_not_installed.json", status, answer)
        self.assertEqual((answer["data"]["version"], answer["data"]["version_latest"]), (None, "0.25.0"))
        await self.call("POST", "/store/addons/local_hri_garage/install")
        status, answer = await self.call("GET", "/store/addons/local_hri_garage")
        self.assert_like("store_app_installed.json", status, answer)
        self.assertEqual((answer["data"]["version"], answer["data"]["version_latest"]), ("0.25.0", "0.25.0"))

    async def test_a_new_definition_of_an_installed_app(self):
        """The store's version_latest follows the definition; version stays the installed one until the update."""
        folder = self.define()
        await self.call("POST", "/store/reload")
        await self.call("POST", "/store/addons/local_hri_garage/install")
        shutil.rmtree(folder)
        self.define(version="0.25.1")
        await self.call("POST", "/store/reload")
        _, answer = await self.call("GET", "/store/addons/local_hri_garage")
        data = answer["data"]
        self.assertEqual((data["version"], data["version_latest"], data["update_available"]), ("0.25.0", "0.25.1", True))
        _, info = await self.call("GET", "/addons/local_hri_garage/info")
        self.assertEqual((info["data"]["version"], info["data"]["version_latest"]), ("0.25.0", "0.25.1"))

    async def test_a_build_in_the_store(self):
        self.define(channel="git", version="0.0.0-0123456789ab")
        await self.call("POST", "/store/reload")
        await self.call("POST", "/store/addons/local_hri_garage/install")
        status, answer = await self.call("GET", "/store/addons/local_hri_garage")
        self.assert_like("store_app_installed_build.json", status, answer)
        self.assertIs(answer["data"]["build"], True)

    async def test_an_app_the_store_does_not_have(self):
        status, answer = await self.call("GET", "/store/addons/local_hri_nosuch")
        self.assert_like("store_app_missing.json", status, answer)
        self.assertEqual(answer["extra_fields"], {"app": "local_hri_nosuch"})
        status, answer = await self.call("GET", "/addons/local_hri_nosuch/info")
        self.assert_like("app_info_missing.json", status, answer)

    async def test_an_installed_instance_and_the_list(self):
        self.define()
        await self.call("POST", "/store/reload")
        await self.call("POST", "/store/addons/local_hri_garage/install")
        status, answer = await self.call("GET", "/addons/local_hri_garage/info")
        self.assert_like("app_info.json", status, answer)
        status, answer = await self.call("GET", "/addons")
        entry = next(a for a in answer["data"]["addons"] if a["slug"] == "local_hri_garage")
        real = next(a for a in captured("addons.json")["answer"]["data"]["addons"] if a["slug"] == "local_hri_garage")
        self.assertEqual(status, 200)
        self.assertEqual(shape(entry), shape(real))
        for other in answer["data"]["addons"]:
            with self.subTest(slug=other["slug"]):
                self.assertEqual(set(other), set(real))

    async def test_a_detached_instance(self):
        """Its definition gone (a restore without the local apps folder): still installed, detached, no store entry."""
        folder = self.define()
        await self.call("POST", "/store/reload")
        await self.call("POST", "/store/addons/local_hri_garage/install")
        shutil.rmtree(folder)
        await self.call("POST", "/store/reload")
        status, answer = await self.call("GET", "/addons/local_hri_garage/info")
        self.assert_like("app_info_detached.json", status, answer)
        data, real = answer["data"], captured("app_info_detached.json")["answer"]["data"]
        for key in ("detached", "update_available", "long_description", "icon", "logo", "changelog", "documentation"):
            self.assertEqual(data[key], real[key], key)
        self.assertEqual(data["version_latest"], data["version"])
        status, answer = await self.call("GET", "/store/addons/local_hri_garage")
        self.assert_like("store_app_missing.json", status, answer)
        _, listing = await self.call("GET", "/addons")
        entry = next(a for a in listing["data"]["addons"] if a["slug"] == "local_hri_garage")
        self.assertEqual((entry["detached"], entry["version_latest"], entry["update_available"]), (True, "0.25.0", False))


class CapturedAnswersTest(unittest.TestCase):
    """What the fixtures say, pinned: the meaning the manager's code depends on."""

    def test_store_version_is_the_installed_version(self):
        missing = captured("store_app_not_installed.json")["answer"]["data"]
        self.assertEqual((missing["installed"], missing["version"]), (False, None))
        self.assertIsInstance(missing["version_latest"], str)
        installed = captured("store_app_installed.json")["answer"]["data"]
        self.assertEqual((installed["installed"], installed["version"]), (True, installed["version_latest"]))

    def test_nothing_private_in_the_fixtures(self):
        for name in sorted(os.listdir(SUPERVISOR_FIXTURES)):
            text = json.dumps(captured(name))
            with self.subTest(fixture=name):
                self.assertNotIn("password", json.dumps(captured(name)["answer"].get("data", {}).get("options", {})))
                for key in ("ingress_entry", "ingress_url"):
                    for value in re.findall(rf'"{key}": "([^"]*)"', text):
                        self.assertRegex(value, r"^/api/hassio_ingress/0+/?$")


if __name__ == "__main__":
    unittest.main()
