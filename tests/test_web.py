"""The HTTP gate: only the Supervisor's transport peer, only the allowed Home Assistant users, state changes only as
fetch + JSON; the policy headers on every answer; the page and its assets."""

import re
import unittest

from hrimgr import web as mgrweb

from .env import USER, Env
from .helpers import tmpdir


class GuardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()

    async def asyncTearDown(self):
        await self.env.close()

    async def client_with(self, **settings):
        return await self.env.client_with(self, headers=USER, **settings)

    async def test_only_the_supervisor_peer(self):
        client = await self.client_with(peers=frozenset({"172.30.32.2"}))
        for path in ("/", "/api/instances", "/api/status", "/static/mgr.js"):
            resp = await client.get(path, headers={"X-Forwarded-For": "172.30.32.2", "X-Real-IP": "172.30.32.2", "Forwarded": "for=172.30.32.2"})
            with self.subTest(path=path):
                self.assertEqual(resp.status, 403)
                self.assertIn("Content-Security-Policy", resp.headers)
        resp = await client.post("/api/instances", json={"name": "x"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.env.stub.calls, [])

    async def test_healthz_answers_the_loopback_only(self):
        client = await self.client_with(peers=frozenset({"172.30.32.2"}))
        resp = await client.get("/healthz")
        self.assertEqual((resp.status, await resp.text()), (200, "ok"))
        resp = await client.post("/healthz", headers={"X-Requested-With": "fetch"}, json={})
        self.assertEqual(resp.status, 403)

    async def test_allowed_users(self):
        client = await self.client_with(allowed_users=frozenset({"alice"}))
        for user, status in (("alice", 200), ("ALICE", 200), ("bob", 403), ("", 403), ("alice ", 403)):
            with self.subTest(user=user):
                resp = await client.get("/", headers={"X-Remote-User-Name": user})
                self.assertEqual(resp.status, status)

    async def test_state_changes_need_fetch_and_json(self):
        client = self.env.client
        cases = [
            ({}, {"json": {"name": "Bad"}}, 403),
            ({"X-Requested-With": "XMLHttpRequest"}, {"json": {"name": "Bad"}}, 403),
            ({"X-Requested-With": "fetch"}, {"data": "name=Bad", "headers_ct": "application/x-www-form-urlencoded"}, 415),
            ({"X-Requested-With": "fetch"}, {"data": '{"name": "Bad"}', "headers_ct": "text/plain"}, 415),
            ({"X-Requested-With": "fetch"}, {"json": {"name": "Bad"}}, 400),
        ]
        for headers, kw, status in cases:
            headers = dict(headers)
            if "headers_ct" in kw:
                headers["Content-Type"] = kw.pop("headers_ct")
            with self.subTest(headers=headers, status=status):
                resp = await client.post("/api/instances", headers=headers, **kw)
                self.assertEqual(resp.status, status)
        resp = await client.delete("/api/instances/garage")
        self.assertEqual(resp.status, 403)
        resp = await client.put("/api/instances", json={}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(resp.status, 405)
        self.assertEqual(self.env.changing_calls(), [])

    async def test_policy_headers_everywhere(self):
        client = self.env.client
        for path, status in (("/", 200), ("/api/status", 200), ("/static/mgr.css", 200), ("/static/nope.js", 404), ("/nope", 404),
                             ("/api/jobs/0123456789abcdef", 404)):
            resp = await client.get(path)
            with self.subTest(path=path):
                self.assertEqual(resp.status, status)
                self.assertEqual(resp.headers.get("Content-Security-Policy"), mgrweb.CSP)
                self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("script-src 'self'", mgrweb.CSP)
        self.assertIn("frame-ancestors 'self'", mgrweb.CSP)
        self.assertNotIn("unsafe", mgrweb.CSP)

    async def test_the_page_and_its_assets(self):
        resp = await self.env.client.get("/")
        html = await resp.text()
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
        self.assertIn(f'href="static/mgr.css?v={mgrweb.ASSET_VERSION}"', html)
        self.assertIn(f'src="static/mgr.js?v={mgrweb.ASSET_VERSION}"', html)
        self.assertNotIn("__V__", html)
        resp = await self.env.client.get(f"/static/mgr.js?v={mgrweb.ASSET_VERSION}")
        self.assertIn("immutable", resp.headers["Cache-Control"])
        self.assertEqual(resp.headers["Content-Type"], "application/javascript")
        for path in ("/static/../web.py", "/static/index.html", "/static/%2e%2e%2fweb.py"):
            resp = await self.env.client.get(path)
            self.assertIn(resp.status, (403, 404), path)

    async def test_no_inline_script_or_style(self):
        """The policy has no 'unsafe-inline': the page may not rely on inline scripts, handlers or style attributes."""
        with open(mgrweb.os.path.join(mgrweb.STATIC_DIR, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        self.assertIsNone(re.search(r"\sstyle\s*=", html))
        self.assertNotIn("<style", html)
        with open(mgrweb.os.path.join(mgrweb.STATIC_DIR, "mgr.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIsNone(re.search(r"style=\\?[\"']", js))
        self.assertNotIn("eval(", js)
        self.assertNotIn("new Function", js)


if __name__ == "__main__":
    unittest.main()
