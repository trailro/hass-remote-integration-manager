"""Settings from the app's options and environment; development mode cannot be reached from the app."""

import io
import json
import logging
import os
import unittest

from hrimgr import settings as settings_mod
from hrimgr.settings import Redact, RedactingFormatter, SettingsError, from_environment

from .helpers import tmpdir


class SettingsTest(unittest.TestCase):
    def test_the_app(self):
        s = from_environment({"SUPERVISOR_TOKEN": "abcdef123456"})
        self.assertFalse(s.dev)
        self.assertEqual(s.peers, frozenset({"172.30.32.2"}))
        self.assertEqual((s.supervisor_url, s.local_apps, s.data_dir, s.port), ("http://supervisor", "/local_apps", "/data", 8099))
        self.assertEqual((s.github_api, s.codeload), ("https://api.github.com", "https://codeload.github.com"))

    def test_no_token_no_start(self):
        with self.assertRaises(SettingsError):
            from_environment({})

    def test_options(self):
        data = tmpdir(self)
        with open(os.path.join(data, "options.json"), "w") as fh:
            json.dump({"allowed_users": ["Alice", " bob ", "", 3], "github_token": "ghp_x123456", "debug": True}, fh)
        env = {"SUPERVISOR_TOKEN": "t" * 10, "HRI_MANAGER_DEV_PEERS": "127.0.0.1", "HRI_MANAGER_DEV_SUPERVISOR_URL": "http://stub:1/sv",
               "HRI_MANAGER_DEV_DATA": data}
        s = from_environment(env)
        self.assertEqual(s.allowed_users, frozenset({"alice", "bob"}))
        self.assertEqual(s.github_token, "ghp_x123456")
        self.assertTrue(s.debug)
        self.assertIn("ghp_x123456", s.secrets)

    def test_development_mode_needs_both_variables_and_a_fake_supervisor(self):
        base = {"SUPERVISOR_TOKEN": "t" * 10}
        for extra in ({"HRI_MANAGER_DEV_PEERS": "127.0.0.1"}, {"HRI_MANAGER_DEV_SUPERVISOR_URL": "http://stub:8080/sv"},
                      {"HRI_MANAGER_DEV_LOCAL_APPS": "/tmp/x"},
                      {"HRI_MANAGER_DEV_PEERS": "127.0.0.1", "HRI_MANAGER_DEV_SUPERVISOR_URL": "http://supervisor"},
                      {"HRI_MANAGER_DEV_PEERS": "127.0.0.1", "HRI_MANAGER_DEV_SUPERVISOR_URL": "http://supervisor/"},
                      {"HRI_MANAGER_DEV_PEERS": "127.0.0.1", "HRI_MANAGER_DEV_SUPERVISOR_URL": "http://supervisor:80"},
                      {"HRI_MANAGER_DEV_PEERS": "not-an-ip", "HRI_MANAGER_DEV_SUPERVISOR_URL": "http://stub:8080/sv"},
                      {"HRI_MANAGER_DEV_PEERS": "127.0.0.1", "HRI_MANAGER_DEV_SUPERVISOR_URL": "http://stub:8080/sv", "HRI_MANAGER_DEV_TYPO": "1"}):
            with self.subTest(extra=extra), self.assertRaises(SettingsError):
                from_environment({**base, **extra})
        s = from_environment({**base, "HRI_MANAGER_DEV_PEERS": "198.51.100.30, ::1", "HRI_MANAGER_DEV_SUPERVISOR_URL": "http://stub:8080/sv"})
        self.assertTrue(s.dev)
        self.assertEqual(s.peers, frozenset({"198.51.100.30", "::1"}))

    def test_github_overrides_only_in_development_mode(self):
        self.assertEqual(from_environment({"SUPERVISOR_TOKEN": "t" * 10}).github_api, "https://api.github.com")
        self.assertTrue(all(v in settings_mod.DEV_VARS for v in ("GITHUB_API", "CODELOAD", "LOCAL_APPS", "DATA", "PORT")))

    def test_tokens_never_reach_the_log(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "calling with %s and %s", ("tok3n-supervisor", "ghp_abcdef"), None)
        Redact(("tok3n-supervisor", "ghp_abcdef")).filter(record)
        self.assertEqual(record.getMessage(), "calling with *** and ***")

    def test_tokens_are_not_in_the_settings_repr(self):
        s = settings_mod.Settings(supervisor_token="tok3n-supervisor", github_token="ghp_abcdef123")
        self.assertNotIn("tok3n-supervisor", repr(s))
        self.assertNotIn("ghp_abcdef123", repr(s))

    def test_tracebacks_are_redacted_and_the_filter_never_raises(self):
        secrets = ("tok3n-supervisor",)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.addFilter(Redact(secrets))
        handler.setFormatter(RedactingFormatter(secrets))
        log = logging.getLogger("hri-mgr-test-redact")
        log.addHandler(handler)
        log.propagate = False
        self.addCleanup(log.removeHandler, handler)
        try:
            raise RuntimeError("the Supervisor refused tok3n-supervisor")
        except RuntimeError:
            log.exception("failed")
        log.warning("bad %d arguments with tok3n-supervisor", "x")  # a log call whose message cannot be built
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d", ("x",), None)
        self.assertTrue(Redact(secrets).filter(record))
        out = buf.getvalue()
        self.assertIn("RuntimeError", out)
        self.assertNotIn("tok3n-supervisor", out)
        self.assertIn("could not be formatted", out)


if __name__ == "__main__":
    unittest.main()
