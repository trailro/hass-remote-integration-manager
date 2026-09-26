"""The Supervisor client's allow-list: every call outside it is refused before any network I/O, a call that changes
an app needs the Managed (checked marker) of that app's slug, bodies carry only the listed keys and values, and what
an app's info holds beyond the listed fields (its options: an instance's password) never leaves the client."""

import asyncio
import json
import os
import threading
import unittest
from unittest import mock

from hrimgr import children, supervisor
from hrimgr.supervisor import NotAllowed, SupervisorClient, authorize

from .fakes.stub import captured
from .helpers import make_child, new_registry, tmpdir

# the allow-list, pinned: a rule added or widened must be added here too, with a reason in supervisor.py
EXPECTED_RULES = {
    ("GET", r"/addons"),
    ("GET", r"/addons/self/info"),
    ("GET", r"/supervisor/info"),
    ("GET", r"/info"),
    ("POST", r"/store/reload"),
    ("GET", r"/store/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})"),
    ("POST", r"/store/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/install"),
    ("POST", r"/store/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/update"),
    ("GET", r"/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/info"),
    ("POST", r"/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/options"),
    ("POST", r"/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/start"),
    ("POST", r"/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/stop"),
    ("POST", r"/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/restart"),
    ("POST", r"/addons/(?P<slug>local_hri_[a-z][a-z0-9_]{0,19})/uninstall"),
}

READ_ONLY_OK = ["/addons", "/addons/self/info", "/supervisor/info", "/info", "/store/addons/local_hri_garage",
                "/addons/local_hri_garage/info"]

# what the manager role could do and the manager must not: other apps, the host, backups, the store's repositories,
# the v2 API, other methods, and path tricks around the allowed shapes
FORBIDDEN = [
    ("POST", "/addons/core_ssh/stop"), ("POST", "/addons/core_mosquitto/uninstall"),
    ("POST", "/addons/5c53de3b_hass_remote_integration/restart"), ("GET", "/addons/core_ssh/info"),
    ("GET", "/addons/5c53de3b_hass_remote_integration/info"),
    ("POST", "/store/addons/core_ssh/install"), ("POST", "/store/addons/local_other/install"),
    ("POST", "/addons/local_hri_garage/rebuild"), ("POST", "/addons/local_hri_garage/stdin"),
    ("POST", "/addons/local_hri_garage/security"), ("POST", "/addons/local_hri_garage/sys_options"),
    ("GET", "/addons/local_hri_garage/logs"), ("GET", "/addons/local_hri_garage/options/config"),
    ("POST", "/addons/local_hri_garage/install"), ("POST", "/addons/local_hri_garage/update"),
    ("POST", "/addons/reload"), ("POST", "/host/reboot"), ("POST", "/host/shutdown"), ("POST", "/os/datadisk/wipe"),
    ("POST", "/backups/new/full"), ("POST", "/backups/abc/restore/full"), ("DELETE", "/backups/abc"),
    ("GET", "/backups"), ("POST", "/store/repositories"), ("DELETE", "/store/repositories/abc"),
    ("POST", "/supervisor/update"), ("POST", "/core/restart"), ("POST", "/core/api/services/x/y"),
    ("GET", "/homeassistant/api/states"), ("POST", "/auth"), ("GET", "/auth/list"), ("POST", "/docker/registries"),
    ("POST", "/v2/apps/local_hri_garage/start"), ("GET", "/v2/apps"), ("POST", "/store/apps/local_hri_garage/install"),
    ("GET", "/addons/"), ("GET", "/addons//info"), ("GET", "/store/addons/local_hri_garage/changelog"),
    ("GET", "/addons/local_hri_garage/info/"), ("GET", "/addons/local_hri_garage/info?x=1"),
    ("POST", "/addons/local_hri_garage/../core_ssh/stop"), ("POST", "/addons/local_hri_garage/%2e%2e/core_ssh/stop"),
    ("POST", "/addons/local_hri_Garage/start"), ("POST", "/addons/local_hri_/start"),
    ("POST", "/addons/local_hri_garage_and_a_very_long_name/start"), ("POST", "/addons/local_hri_1abc/start"),
    ("POST", "addons/local_hri_garage/start"), ("POST", "//addons/local_hri_garage/start"),
    ("POST", "http://evil/addons/local_hri_garage/start"), ("POST", "/addons/local_hri_garage/start\n"),
    ("DELETE", "/addons/local_hri_garage"), ("PUT", "/addons/local_hri_garage/options"),
    ("PATCH", "/addons/local_hri_garage/options"), ("HEAD", "/addons"), ("GET", "/addons/local_hri_garage/start"),
    ("POST", "/addons"), ("POST", "/addons/local_hri_garage/info"), ("GET", "/store/reload"),
    ("POST", "/addons/self/options"), ("POST", "/addons/self/restart"), ("POST", "/addons/self/uninstall"),
    ("POST", "/store/addons/self/update"), ("GET", "/network/info"), ("GET", "/host/info"), ("GET", "/jobs/info"),
]


class FakeContent:
    def __init__(self, body: bytes, size: int = 7):
        self.body, self.size = body, size

    async def iter_chunked(self, n):
        for i in range(0, len(self.body), self.size):  # in small pieces, as a network delivers it
            yield self.body[i:i + self.size]


class FakeResponse:
    def __init__(self, status=200, body=b'{"result":"ok","data":{}}'):
        self.status = status
        self.content_length = None
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class RecordingSession:
    def __init__(self, body=b'{"result":"ok","data":{}}', status=200):
        self.requests = []
        self.body, self.status = body, status

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return FakeResponse(self.status, self.body)

    async def close(self):
        pass


class AllowListTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.registry = new_registry(self)
        make_child(self.root, "garage", registry=self.registry)
        self.garage = children.load_managed(self.root, "garage", self.registry)

    def test_the_rules_are_exactly_the_pinned_ones(self):
        self.assertEqual({(r.method, r.pattern.pattern) for r in supervisor.RULES}, EXPECTED_RULES)

    def test_the_bodies_and_their_required_keys_are_pinned(self):
        bodies = {r.pattern.pattern.rsplit("/", 1)[-1]: (dict(r.body), r.required) for r in supervisor.RULES if r.body}
        self.assertEqual(bodies, {
            "update": ({"backup": (True, False)}, ()),
            "options": ({"boot": ("auto",), "watchdog": (True, False), "ingress_panel": (True, False)}, ()),
            "uninstall": ({"remove_config": (True, False)}, ("remove_config",)),
        })

    def test_read_only_calls_pass_without_an_instance(self):
        for path in READ_ONLY_OK:
            with self.subTest(path=path):
                self.assertFalse(authorize("GET", path).changes_app)
        self.assertFalse(authorize("POST", "/store/reload").changes_app)

    def test_every_call_outside_the_list_is_refused(self):
        for method, path in FORBIDDEN:
            for managed in (None, self.garage):
                with self.subTest(method=method, path=path, managed=managed is not None):
                    with self.assertRaises(NotAllowed):
                        authorize(method, path, None if method == "GET" else {}, managed)

    def test_refused_calls_never_reach_the_network(self):
        session = RecordingSession()
        client = SupervisorClient("http://supervisor", "tok3n-secret", session=session)

        async def run():
            for method, path in FORBIDDEN:
                for managed in (None, self.garage):
                    with self.assertRaises(NotAllowed):
                        await client.call(method, path, managed=managed)

        asyncio.run(run())
        self.assertEqual(session.requests, [])

    def test_changing_calls_need_the_managed_of_that_slug(self):
        make_child(self.root, "attic", registry=self.registry)
        attic = children.load_managed(self.root, "attic", self.registry)
        for path in ("/store/addons/local_hri_garage/install", "/store/addons/local_hri_garage/update",
                     "/addons/local_hri_garage/options", "/addons/local_hri_garage/start", "/addons/local_hri_garage/stop",
                     "/addons/local_hri_garage/restart", "/addons/local_hri_garage/uninstall"):
            with self.subTest(path=path):
                with self.assertRaises(NotAllowed):
                    authorize("POST", path, {}, None)
                with self.assertRaises(NotAllowed):
                    authorize("POST", path, {}, attic)
                with self.assertRaises(NotAllowed):  # something that only looks like a Managed
                    authorize("POST", path, {}, mock.Mock(slug="local_hri_garage", verify=lambda: None))
                body = {"remove_config": False} if path.endswith("/uninstall") else {}  # a required key
                self.assertTrue(authorize("POST", path, body, self.garage).changes_app)

    def test_a_marker_gone_since_loading_refuses_the_call(self):
        os.unlink(os.path.join(self.root, "hri_garage", children.MARKER))
        session = RecordingSession()
        client = SupervisorClient("http://supervisor", "t", session=session)
        with self.assertRaises(NotAllowed):
            asyncio.run(client.start(self.garage))
        self.assertEqual(session.requests, [])

    def test_a_marker_changed_to_another_slug_refuses_the_call(self):
        with open(os.path.join(self.root, "hri_garage", children.MARKER), "w") as fh:
            fh.write('{"manager": "hri_manager", "name": "garage", "slug": "core_ssh", "channel": "release", '
                     '"instance_id": "%s"}' % self.garage.marker["instance_id"])
        with self.assertRaises(NotAllowed):
            authorize("POST", "/addons/local_hri_garage/stop", {}, self.garage)

    def test_a_marker_the_registry_does_not_hold_refuses_the_call(self):
        """A marker anyone with write access to the local apps folder could forge: without the registry's entry of
        the same instance id, in the manager's own /data, no changing call passes."""
        self.registry.update("garage", instance_id="f" * 32)
        with self.assertRaises(NotAllowed):
            authorize("POST", "/addons/local_hri_garage/uninstall", {"remove_config": True}, self.garage)
        self.registry.remove("garage")
        with self.assertRaises(NotAllowed):
            authorize("POST", "/addons/local_hri_garage/uninstall", {"remove_config": True}, self.garage)

    def test_read_only_calls_take_no_instance(self):
        with self.assertRaises(NotAllowed):
            authorize("GET", "/addons/local_hri_garage/info", None, self.garage)

    def test_bodies_carry_only_the_listed_keys_and_values(self):
        g = self.garage
        authorize("POST", "/addons/local_hri_garage/options", {"boot": "auto", "watchdog": True, "ingress_panel": True}, g)
        authorize("POST", "/addons/local_hri_garage/uninstall", {"remove_config": False}, g)
        authorize("POST", "/store/addons/local_hri_garage/update", {"backup": False}, g)
        refused = [
            ("/addons/local_hri_garage/options", {"options": {"password": "x"}}),
            ("/addons/local_hri_garage/options", {"network": {"8087/tcp": 8087}}),
            ("/addons/local_hri_garage/options", {"boot": "manual"}),
            ("/addons/local_hri_garage/options", {"watchdog": 1}),
            ("/addons/local_hri_garage/options", {"watchdog": "true"}),
            ("/addons/local_hri_garage/options", {"auto_update": True}),
            ("/addons/local_hri_garage/start", {"x": 1}),
            ("/addons/local_hri_garage/uninstall", {"remove_config": "yes"}),
            ("/addons/local_hri_garage/uninstall", {}),  # remove_config is required: never the Supervisor's default
            ("/addons/local_hri_garage/uninstall", None),
            ("/store/addons/local_hri_garage/install", {"background": True}),
            ("/addons/local_hri_garage/options", ["boot"]),
        ]
        for path, body in refused:
            with self.subTest(path=path, body=body):
                with self.assertRaises(NotAllowed):
                    authorize("POST", path, body, g)
        with self.assertRaises(NotAllowed):
            authorize("GET", "/addons", {"x": 1})

    def test_the_client_methods_send_what_the_rules_allow(self):
        session = RecordingSession()
        client = SupervisorClient("http://supervisor", "t", session=session)
        g = self.garage

        async def run():
            await client.reload_store()
            await client.install(g)
            await client.set_options(g, boot="auto", watchdog=True, ingress_panel=True)
            await client.start(g)
            await client.stop(g)
            await client.restart(g)
            await client.update(g)
            await client.uninstall(g, remove_config=True)

        asyncio.run(run())
        sent = [(m, u.replace("http://supervisor", ""), kw.get("json")) for m, u, kw in session.requests]
        self.assertEqual(sent, [
            ("POST", "/store/reload", {}),
            ("POST", "/store/addons/local_hri_garage/install", {}),
            ("POST", "/addons/local_hri_garage/options", {"boot": "auto", "watchdog": True, "ingress_panel": True}),
            ("POST", "/addons/local_hri_garage/start", {}),
            ("POST", "/addons/local_hri_garage/stop", {}),
            ("POST", "/addons/local_hri_garage/restart", {}),
            ("POST", "/store/addons/local_hri_garage/update", {"backup": False}),
            ("POST", "/addons/local_hri_garage/uninstall", {"remove_config": True}),
        ])
        for _, _, kw in session.requests:
            self.assertEqual(kw["headers"], {"Authorization": "Bearer t"})
            self.assertFalse(kw["allow_redirects"])

    def test_the_marker_and_registry_are_read_off_the_event_loop(self):
        client = SupervisorClient("http://supervisor", "t", session=RecordingSession())
        load = children.load_managed
        threads = []

        def recording(*args, **kw):
            threads.append(threading.current_thread() is threading.main_thread())
            return load(*args, **kw)

        with mock.patch.object(children, "load_managed", side_effect=recording):
            asyncio.run(client.start(self.garage))
        self.assertEqual(threads, [False])

    def test_every_request_goes_through_authorize(self):
        """No method of the client talks to the session by itself."""
        session = RecordingSession(body=b'{"result":"ok","data":{"addons":[]}}')
        client = SupervisorClient("http://supervisor", "t", session=session)
        seen = []
        real = supervisor.authorize

        def spy(method, path, body=None, managed=None):
            seen.append((method, path))
            return real(method, path, body, managed)

        async def run():
            await client.list_apps()
            await client.self_info()
            await client.supervisor_info()
            await client.host_info()
            await client.app_info("local_hri_garage")
            await client.store_app("local_hri_garage")
            await client.reload_store()
            for fn in (client.install, client.update, client.start, client.stop, client.restart):
                await fn(self.garage)
            await client.set_options(self.garage, boot="auto")
            await client.uninstall(self.garage, False)

        with mock.patch.object(supervisor, "authorize", spy):
            asyncio.run(run())
        self.assertEqual(len(seen), len(session.requests))
        self.assertEqual(len(seen), 14)

    def test_info_never_carries_the_options(self):
        body = (b'{"result":"ok","data":{"slug":"local_hri_garage","version":"0.25.0","state":"started",'
                b'"options":{"password":"child-secret"},"schema":[],"ip_address":"198.51.100.4","ingress_url":"/api/hassio_ingress/x/"}}')
        client = SupervisorClient("http://supervisor", "t", session=RecordingSession(body=body))
        info = asyncio.run(client.app_info("local_hri_garage"))
        self.assertNotIn("options", info)
        self.assertNotIn("child-secret", repr(info))
        self.assertEqual(set(info), {"slug", "version", "state", "ingress_url"})
        me = asyncio.run(SupervisorClient("http://s", "t", session=RecordingSession(
            body=b'{"result":"ok","data":{"hassio_role":"manager","options":{"github_token":"gh-secret"}}}')).self_info())
        self.assertEqual(me, {"hassio_role": "manager"})

    def test_answers_are_read_to_their_end_and_capped(self):
        body = b'{"result":"ok","data":{"addons":[' + b",".join(b'{"slug":"a%d"}' % i for i in range(200)) + b"]}}"
        apps = asyncio.run(SupervisorClient("http://s", "t", session=RecordingSession(body=body)).list_apps())
        self.assertEqual(len(apps), 200)
        with mock.patch.object(supervisor, "MAX_ANSWER", 100), self.assertRaises(supervisor.SupervisorError):
            asyncio.run(SupervisorClient("http://s", "t", session=RecordingSession(body=body)).list_apps())

    def test_an_error_that_quotes_the_options_is_replaced(self):
        """The Supervisor's text for invalid options ends with ". Got {options}": an instance's password."""
        g = self.garage
        bodies = [
            b'{"result":"error","message":"App local_hri_garage has invalid options: expected bool. Got {\'password\': \'child-secret\'}",'
            b'"error_key":"app_configuration_invalid_error"}',
            b'{"result":"error","message":"App has invalid options: x. Got {\'password\': \'child-secret\'}"}',
            b'{"result":"error","message":"child-secret","error_key":"addon_configuration_invalid_error"}',
            b'{"result":"error","message":"x. Got child-secret","error_key":["not", "a", "string"]}',
        ]
        for body in bodies:
            client = SupervisorClient("http://s", "t", session=RecordingSession(status=400, body=body))
            with self.subTest(body=body[:60]), self.assertRaises(supervisor.SupervisorError) as ctx:
                asyncio.run(client.start(g))
            text = str(ctx.exception)
            self.assertNotIn("child-secret", text)
            self.assertNotIn("Got", text)
            self.assertIn("local_hri_garage", text)
            self.assertIn("configuration", text)

    def test_errors_are_reported_with_the_supervisor_message(self):
        client = SupervisorClient("http://s", "t", session=RecordingSession(status=400, body=b'{"result":"error","message":"App is not installed"}'))
        with self.assertRaises(supervisor.SupervisorError) as ctx:
            asyncio.run(client.app_info("local_hri_garage"))
        self.assertIn("App is not installed", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 400)
        missing = SupervisorClient("http://s", "t", session=RecordingSession(status=404, body=b'{"result":"error","message":"App does not exist"}'))
        self.assertIsNone(asyncio.run(missing.store_app("local_hri_garage")))

    def test_the_store_entry_as_a_real_supervisor_sends_it(self):
        """GET /store/addons/<slug> of Supervisor 2026.09.2, captured: its version is the INSTALLED version (null
        before the install); the definition's is version_latest, which is what the manager waits for."""
        for fixture, latest in (("store_app_not_installed.json", "0.25.0"), ("store_app_installed.json", "0.25.0")):
            real = captured(fixture)
            session = RecordingSession(status=real["status"], body=json.dumps(real["answer"]).encode())
            entry = asyncio.run(SupervisorClient("http://s", "t", session=session).store_app("local_hri_garage"))
            with self.subTest(fixture=fixture):
                self.assertEqual(entry["version_latest"], latest)
                self.assertNotIn("version", entry)  # the installed version, under a name that invites the mistake
        real = captured("store_app_missing.json")
        session = RecordingSession(status=real["status"], body=json.dumps(real["answer"]).encode())
        self.assertIsNone(asyncio.run(SupervisorClient("http://s", "t", session=session).store_app("local_hri_nosuch")))


if __name__ == "__main__":
    unittest.main()
