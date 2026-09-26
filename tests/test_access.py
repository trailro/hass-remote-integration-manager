"""Only Home Assistant's administrators use the manager: the ingress headers say who asks, Core says whether that user
is an administrator (config/auth/list through the Supervisor's proxy), and anything unclear is refused (403)."""

import asyncio
import unittest

from hrimgr import corews
from hrimgr.corews import CoreError, CoreUsers, NotAllowedMessage, check_message, parse_users

from .env import Env
from .fakes.stub import ALICE_ID, BOB_ID, DAVE_ID, OLGA_ID, core_user
from .helpers import tmpdir

ROUTES = [("GET", "/"), ("GET", "/static/mgr.js"), ("GET", "/api/status"), ("GET", "/api/instances"),
          ("GET", "/api/releases"), ("GET", "/api/jobs"), ("POST", "/api/instances"),
          ("POST", "/api/instances/garage/start"), ("DELETE", "/api/instances/garage")]


def ingress(user_id=None, name=None) -> dict:
    headers = {}
    if user_id is not None:
        headers["X-Remote-User-Id"] = user_id
    if name is not None:
        headers["X-Remote-User-Name"] = name
    return headers


class AccessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await Env(tmpdir(self)).start()

    async def asyncTearDown(self):
        await self.env.close()

    async def status_of(self, client, method, path, headers=None):
        extra = {"X-Requested-With": "fetch"} if method != "GET" else {}
        resp = await client.request(method, path, headers={**(headers or {}), **extra},
                                    json={} if method != "GET" else None)
        return resp.status, await resp.text()

    async def test_administrators_are_served(self):
        client = await self.env.client_with(self)
        for uid, name in ((ALICE_ID, "alice"), (OLGA_ID, "olga"), (ALICE_ID, None)):
            with self.subTest(user=name):
                status, _ = await self.status_of(client, "GET", "/api/status", ingress(uid, name))
                self.assertEqual(status, 200)

    async def test_everyone_else_gets_403_on_every_route(self):
        client = await self.env.client_with(self)
        users = {"a user": ingress(BOB_ID, "bob"), "a deactivated administrator": ingress(DAVE_ID, "dave"),
                 "no user id": ingress(None, "alice"), "no headers": {}, "an unknown id": ingress("f" * 32, "alice"),
                 "an empty id": ingress("", "alice")}
        for label, headers in users.items():
            for method, path in ROUTES:
                with self.subTest(who=label, route=f"{method} {path}"):
                    status, text = await self.status_of(client, method, path, headers)
                    self.assertEqual(status, 403, text)
        self.assertEqual(self.env.changing_calls(), [])

    async def raw_status(self, client, lines: list[str]) -> int:
        """A GET with these header lines exactly (aiohttp's client would merge two spellings of one header)."""
        reader, writer = await asyncio.open_connection(client.host, client.port)
        writer.write(("GET /api/status HTTP/1.1\r\nHost: x\r\nConnection: close\r\n" + "".join(f"{h}\r\n" for h in lines)
                      + "\r\n").encode())
        await writer.drain()
        status = int((await reader.readline()).split()[1])
        writer.close()
        await writer.wait_closed()
        return status

    async def test_a_second_id_or_name_header_is_refused(self):
        """The Supervisor sets X-Remote-User-Id first and drops a client's own only in its exact spelling: a
        lowercase copy from the browser arrives as a second value."""
        client = await self.env.client_with(self)
        self.assertEqual(await self.raw_status(client, [f"X-Remote-User-Id: {ALICE_ID}", "X-Remote-User-Name: alice"]), 200)
        for lines in ([f"X-Remote-User-Id: {BOB_ID}", f"x-remote-user-id: {ALICE_ID}"],
                      [f"X-Remote-User-Id: {ALICE_ID}", f"x-remote-user-id: {ALICE_ID}"],
                      [f"X-Remote-User-Id: {ALICE_ID}", "X-Remote-User-Name: alice", "x-remote-user-name: root"]):
            with self.subTest(lines=lines):
                self.assertEqual(await self.raw_status(client, lines), 403)

    async def test_the_user_headers_must_be_spelled_as_the_supervisor_spells_them(self):
        """What the Supervisor 2026.09.3 sends (its _init_header through its aiohttp, bytes captured): its own
        X-Remote-User-Id and X-Remote-User-Name, spelled so.  A client's x-remote-user-id passes its filter, and
        aiohttp's session merges the two spellings keeping the client's: ONE header arrives, the client's value in
        the client's spelling.  So any other spelling is refused, even alone."""
        client = await self.env.client_with(self)
        ok = [f"X-Remote-User-Id: {ALICE_ID}", "X-Remote-User-Name: alice", "X-Remote-User-Display-Name: Alice"]
        self.assertEqual(await self.raw_status(client, ok), 200)
        self.assertEqual(await self.raw_status(client, [f"X-Remote-User-Id: {ALICE_ID}"]), 200)  # a user without a login name
        for lines in ([f"x-remote-user-id: {ALICE_ID}"],  # bob's session, alice's id injected: what reaches the app
                      [f"X-REMOTE-USER-ID: {ALICE_ID}", "X-Remote-User-Name: alice"],
                      [f"X-Remote-user-Id: {ALICE_ID}"],
                      [f"X-Remote-User-Id: {ALICE_ID}", "x-remote-user-name: root"],
                      [f"X-Remote-User-Id: {ALICE_ID}", "X-Remote-User-Name: alice", "X-Remote-User-Name: alice"],
                      [f"X-Remote-User-Id: {ALICE_ID}", f"X-Remote-User-Id: {ALICE_ID}"]):
            with self.subTest(lines=lines):
                self.assertEqual(await self.raw_status(client, lines), 403)

    async def test_the_page_says_why(self):
        client = await self.env.client_with(self)
        status, text = await self.status_of(client, "GET", "/", ingress(BOB_ID, "bob"))
        self.assertEqual(status, 403)
        self.assertIn("administrators", text)

    async def test_fails_closed_when_core_cannot_answer(self):
        client = await self.env.client_with(self)
        cases = {
            "Core down": lambda s: setattr(s, "core_down", True),
            "not a list": lambda s: setattr(s, "users", {"a": 1}),
            "no is_owner": lambda s: setattr(s, "users", [{k: v for k, v in core_user(ALICE_ID, "alice", groups=("system-admin",)).items() if k != "is_owner"}]),
            "is_admin as a string": lambda s: setattr(s, "users", [{**core_user(ALICE_ID, "alice"), "is_owner": "true"}]),
            "an id twice": lambda s: setattr(s, "users", [core_user(ALICE_ID, "alice", groups=("system-admin",))] * 2),
        }
        for label, spoil in cases.items():
            self.env.stub.users = [core_user(ALICE_ID, "alice", groups=("system-admin",))]
            self.env.stub.core_down = False
            spoil(self.env.stub)
            self.env.users._users = None  # nothing cached: the answer decides
            with self.subTest(case=label):
                status, text = await self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, "alice"))
                self.assertEqual(status, 403, text)
                self.assertIn("could not check", text)

    async def test_the_answer_is_cached_and_refreshed_once_for_an_unknown_id(self):
        client = await self.env.client_with(self)
        users = self.env.users
        await self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, "alice"))
        base = users.fetches
        for _ in range(3):
            status, _ = await self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, "alice"))
            self.assertEqual(status, 200)
        self.assertEqual(users.fetches, base)  # within the TTL: no new question
        carol = "ca401000000000000000000000000ca4"
        self.env.stub.users.append(core_user(carol, "carol", groups=("system-admin",)))
        status, _ = await self.status_of(client, "GET", "/api/status", ingress(carol, "carol"))
        self.assertEqual(status, 200)  # a new administrator: asked again once
        self.assertEqual(users.fetches, base + 1)
        status, _ = await self.status_of(client, "GET", "/api/status", ingress("e" * 32, "eve"))
        self.assertEqual(status, 403)
        self.assertEqual(users.fetches, base + 2)
        # alice is demoted: seen after the TTL
        self.env.stub.users[0] = core_user(ALICE_ID, "alice")
        users._fetched -= corews.DEFAULT_TTL + 1
        status, _ = await self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, "alice"))
        self.assertEqual(status, 403)

    async def test_concurrent_requests_share_one_question(self):
        client = await self.env.client_with(self)
        results = await asyncio.gather(*(self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, "alice")) for _ in range(5)))
        self.assertEqual([s for s, _ in results], [200] * 5)
        self.assertEqual(self.env.users.fetches, 1)

    async def test_allowed_users_narrows_administrators(self):
        client = await self.env.client_with(self, allowed_users=frozenset({"alice", OLGA_ID.casefold()}))
        for headers, status in ((ingress(ALICE_ID, "alice"), 200), (ingress(ALICE_ID, "ALICE"), 200),
                                (ingress(OLGA_ID, "not-listed"), 200), (ingress(OLGA_ID, None), 200),
                                (ingress(BOB_ID, "alice"), 403), (ingress(DAVE_ID, "alice"), 403)):
            with self.subTest(headers=headers):
                got, _ = await self.status_of(client, "GET", "/api/status", headers)
                self.assertEqual(got, status)
        client = await self.env.client_with(self, allowed_users=frozenset({"olga"}))
        got, _ = await self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, "alice"))
        self.assertEqual(got, 403)  # an administrator, but not listed

    async def test_allowed_users_keys_on_the_verified_id(self):
        """The user name header is not trusted for allowed_users: the id is, with the username and name Core reports
        for that id."""
        client = await self.env.client_with(self, allowed_users=frozenset({"alice"}))
        got, _ = await self.status_of(client, "GET", "/api/status", ingress(OLGA_ID, "alice"))
        self.assertEqual(got, 403)  # olga, an administrator, naming herself alice
        got, _ = await self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, "someone-else"))
        self.assertEqual(got, 200)  # alice's id: Core says her username is alice, whatever the header says
        got, _ = await self.status_of(client, "GET", "/api/status", ingress(ALICE_ID, None))
        self.assertEqual(got, 200)
        client = await self.env.client_with(self, allowed_users=frozenset({"olga"}))  # Core's display name: Olga
        self.env.stub.users[2] = {**core_user(OLGA_ID, None, owner=True, groups=()), "name": "Olga"}
        self.env.users._users = None
        got, _ = await self.status_of(client, "GET", "/api/status", ingress(OLGA_ID, None))
        self.assertEqual(got, 200)  # no login name (an external login): matched by the name Core has

    async def test_only_the_allow_listed_messages_reach_core(self):
        client = await self.env.client_with(self)
        for uid in (ALICE_ID, BOB_ID, "0" * 32):
            await self.status_of(client, "GET", "/api/status", ingress(uid, "x"))
        types = [m.get("type") for m in self.env.stub.ws_messages if isinstance(m, dict)]
        self.assertTrue(types)
        self.assertEqual(set(types), {"auth", "config/auth/list"})
        for m in self.env.stub.ws_messages:
            self.assertIn(set(m), ({"type", "access_token"}, {"type", "id"}))


class CoreClientTest(unittest.TestCase):
    def test_the_allow_list(self):
        check_message({"type": "auth", "access_token": "t"})
        check_message({"id": 1, "type": "config/auth/list"})
        refused = [
            {"type": "config/auth/delete", "id": 2}, {"type": "config/auth/create", "id": 2}, {"type": "call_service", "id": 2},
            {"type": "supervisor/api", "id": 2}, {"type": "hassio/update/addon", "id": 2}, {"type": "auth/current_user", "id": 2},
            {"type": "config/auth/list", "id": 1, "extra": 1}, {"type": "config/auth/list"}, {"type": "config/auth/list", "id": "1"},
            {"type": "config/auth/list", "id": True}, {"type": "auth", "access_token": "t", "x": 1}, {"type": "auth"},
            {"type": "auth", "access_token": ""}, ["config/auth/list"], "config/auth/list", None, {"type": None, "id": 1},
        ]
        for message in refused:
            with self.subTest(message=message), self.assertRaises(NotAllowedMessage):
                check_message(message)

    def test_the_client_sends_through_the_gate(self):
        sent = []

        class WS:
            async def send_str(self, data):
                sent.append(data)

        users = CoreUsers("ws://supervisor/core/websocket", "t")
        with self.assertRaises(NotAllowedMessage):
            asyncio.run(users._send(WS(), {"id": 2, "type": "config/auth/delete", "user_id": "x"}))
        self.assertEqual(sent, [])

    def test_is_admin_follows_core(self):
        users = parse_users([core_user("a", "a", groups=("system-admin",)), core_user("b", "b"),
                             core_user("c", None, owner=True, active=False, groups=()),
                             core_user("d", "d", active=False, groups=("system-admin",)),
                             core_user("e", "e", groups=("system-read-only", "system-admin"))])
        self.assertEqual({k: u.is_admin for k, u in users.items()}, {"a": True, "b": False, "c": True, "d": False, "e": True})

    def test_the_token_is_not_in_its_repr(self):
        self.assertNotIn("s3cret-token", repr(CoreUsers("ws://supervisor/core/websocket", "s3cret-token")))

    def test_unreachable_core_is_an_error(self):
        users = CoreUsers("ws://127.0.0.1:9/core/websocket", "t", timeout=5)

        async def run():
            try:
                with self.assertRaises(CoreError):
                    await users.user(ALICE_ID)
            finally:
                await users.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
