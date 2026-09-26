"""The contract of hrimgr.stamp with HRI's CI: HRI checks its app template with the manager's latest release before
it releases (``hri_manager`` on PYTHONPATH, only PyYAML installed), calling ``stamp.vet_template(data)`` and
``stamp.stamp(template, name, version, channel, bluetooth=...)``.  The module must import with nothing but the
standard library and PyYAML, and a new argument of stamp() must be a keyword with a default."""

import inspect
import subprocess
import sys
import textwrap
import unittest

from hrimgr import stamp

from . import APP_DIR
from .helpers import FIXTURE_0252, FIXTURE_CONFIG

# run in a fresh interpreter: any import outside the standard library and PyYAML fails, as in a CI with only PyYAML
PROBE = textwrap.dedent('''
    import importlib.abc
    import sys

    ALLOWED = set(sys.stdlib_module_names) | {"yaml", "_yaml", "hrimgr"}

    class OnlyPyYAML(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.partition(".")[0] not in ALLOWED:
                raise ImportError(f"{fullname} is neither the standard library nor PyYAML")
            return None

    sys.meta_path.insert(0, OnlyPyYAML())
    sys.path.insert(0, sys.argv[1])

    import yaml
    from hrimgr import stamp

    for path in sys.argv[2:]:
        template = yaml.safe_load(open(path, encoding="utf-8").read())
        stamp.vet_template(template)
        for bluetooth in (False, True):
            out = stamp.stamp(template, "garage", "0.25.0", "release", bluetooth=bluetooth)
            assert out["slug"] == "hri_garage" and out.get("host_dbus", False) is bluetooth, out
            assert "host_network" not in out
        out = stamp.stamp(template, "garage", "0.26.0", "git", bluetooth=False, host_network=True)
        assert (out["host_network"], out["ingress_port"]) == (True, 0), out
    # what came in besides the standard library: PyYAML (and its C extension's runtime) and the manager
    loaded = {m.partition(".")[0] for m in sys.modules} - set(sys.stdlib_module_names) - {"__main__", "sitecustomize"}
    print(",".join(sorted(m for m in loaded if not m.startswith(("cython_runtime", "_cython_")))))
''')


class StampContractTest(unittest.TestCase):
    def test_imports_with_only_pyyaml(self):
        run = subprocess.run([sys.executable, "-I", "-c", PROBE, str(APP_DIR), str(FIXTURE_CONFIG),
                              str(FIXTURE_0252 / "app_config.yaml")], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertLessEqual(set(run.stdout.strip().split(",")), {"yaml", "_yaml", "hrimgr"})

    def test_signature(self):
        params = inspect.signature(stamp.stamp).parameters
        self.assertEqual(list(params)[:4], ["template", "name", "version", "channel"])
        for name in list(params)[4:]:
            with self.subTest(name=name):
                self.assertIsNot(params[name].default, inspect.Parameter.empty, "a new argument needs a default")
                self.assertNotEqual(params[name].kind, inspect.Parameter.VAR_POSITIONAL)
        self.assertEqual(params["bluetooth"].default, False)
        self.assertEqual((params["host_network"].default, params["host_network"].kind),
                         (False, inspect.Parameter.KEYWORD_ONLY))
        self.assertEqual(list(inspect.signature(stamp.vet_template).parameters), ["data"])


if __name__ == "__main__":
    unittest.main()
