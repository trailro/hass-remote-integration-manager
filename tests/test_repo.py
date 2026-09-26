"""The repository as the Supervisor and a reader see it: one app, its config pinned, versions in step, relative URLs
only, development mode unreachable from the app, and nothing private."""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

from hrimgr import VERSION, settings, stamp

from . import APP_DIR, ROOT

CONFIG = yaml.safe_load((APP_DIR / "config.yaml").read_text(encoding="utf-8"))


def repo_files() -> list[pathlib.Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]


# the manager's own versions: semver, pre-releases as X.Y.Z-rcN (docker/metadata-action tags only semver)
RELEASE_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(rc|beta|alpha)\.?(\d+))?")


def release_key(version: str) -> tuple:
    m = RELEASE_VERSION.fullmatch(version)
    if not m:
        raise ValueError(f"{version!r} is not a semver release or pre-release (X.Y.Z or X.Y.Z-rcN)")
    pre = {"alpha": 1, "beta": 2, "rc": 3}.get(m.group(4) or "", 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1, pre, int(m.group(5) or 0))


class ReleaseVersionTest(unittest.TestCase):
    def test_semver_shape(self):
        release_key(VERSION)
        self.assertLess(release_key("0.2.0-rc1"), release_key("0.2.0"))
        self.assertLess(release_key("0.1.1"), release_key("0.2.0-rc1"))
        for bad in ("0.2.0b1", "0.2.0rc1", "v0.2.0", "0.2"):  # HRI's form, or no version at all
            with self.subTest(version=bad), self.assertRaises(ValueError):
                release_key(bad)


class AppConfigTest(unittest.TestCase):
    def test_the_store_finds_one_app(self):
        self.assertEqual(stamp.find_configs(str(ROOT)), ["hri_manager/config.yaml"])

    def test_config(self):
        self.assertEqual(CONFIG["slug"], "hri_manager")
        # the version, and the image: line, are the Image workflow's app-version job's to write, after the image of
        # that version is pushed (tests/test_image_workflow.py): never ahead of the code, and only this image
        self.assertLessEqual(release_key(CONFIG["version"]), release_key(VERSION))
        self.assertIn(CONFIG.get("image"), (None, "ghcr.io/trailro/hass-remote-integration-manager"))
        self.assertEqual((CONFIG["hassio_api"], CONFIG["hassio_role"]), (True, "manager"))
        self.assertEqual(CONFIG["map"], [{"type": "local_apps", "read_only": False}])
        self.assertTrue(CONFIG["ingress"])
        self.assertNotIn("ports", CONFIG)
        self.assertNotIn("webui", CONFIG)
        self.assertEqual(CONFIG["stage"], "experimental")
        self.assertEqual(sorted(CONFIG["arch"]), ["aarch64", "amd64"])
        self.assertIs(CONFIG["homeassistant_api"], True)  # Core's config/auth/list: administrators only (corews.py)
        # the stop grace: the jobs' rollbacks (ROLLBACK_BOUND) and the wait for open requests fit in it
        from hrimgr import __main__ as main_module, instances
        self.assertEqual(CONFIG["timeout"], 30)
        self.assertLess(instances.ROLLBACK_BOUND + main_module.SHUTDOWN_TIMEOUT, CONFIG["timeout"] - 5)
        self.assertLess(instances.CONTAIN_BOUND + main_module.SHUTDOWN_TIMEOUT, CONFIG["timeout"] - 5)
        for key in ("host_network", "privileged", "full_access", "docker_api", "auth_api", "devices", "uart"):
            self.assertNotIn(key, CONFIG)

    def test_development_mode_cannot_be_set_by_the_app(self):
        """The Supervisor sets an app's environment only from config.yaml's environment key; the image sets none."""
        self.assertNotIn("environment", CONFIG)
        dockerfile = (APP_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn(settings.DEV_PREFIX, dockerfile)
        self.assertNotIn(settings.DEV_PREFIX.rstrip("_"), (APP_DIR / "config.yaml").read_text(encoding="utf-8"))
        options = set(CONFIG["options"]) | set(CONFIG["schema"])
        self.assertFalse(any(o.upper().startswith("HRI_MANAGER") for o in options))

    def test_translations_cover_the_options(self):
        tr = yaml.safe_load((APP_DIR / "translations" / "en.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(tr["configuration"]), set(CONFIG["schema"]))

    def test_versions_in_step(self):
        changelog = (APP_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"^## (\S+)", changelog, re.M).group(1), VERSION)
        self.assertEqual(settings.INGRESS_PORT, 8099)  # the Supervisor's default ingress_port, which config.yaml leaves out
        self.assertNotIn("ingress_port", CONFIG)
        self.assertIn("127.0.0.1:8099/healthz", (APP_DIR / "Dockerfile").read_text(encoding="utf-8"))

    def test_the_image_installs_the_hashed_lock(self):
        """requirements.txt is the pip-compile lock of requirements.in: every package pinned with its hashes, the ones
        requirements.in names at their pins, and the image, CI and the tests install it (with --require-hashes)."""
        lock = (APP_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("autogenerated by pip-compile", lock)  # what Dependabot's pip-compile support looks for
        self.assertIn("--output-file=requirements.txt", lock)
        pins = dict(re.findall(r"(?m)^([A-Za-z0-9_.-]+)==(\S+) \\$", lock))
        self.assertGreaterEqual(len(pins), 5)  # aiohttp's own dependencies are in it
        for block in re.split(r"(?m)^(?=[A-Za-z0-9_.-]+==)", lock)[1:]:
            self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}", block.splitlines()[0])
        wanted = {}
        for line in (APP_DIR / "requirements.in").read_text(encoding="utf-8").splitlines():
            name, _, version = line.partition("==")
            wanted[name.lower()] = version
        self.assertEqual({k: v for k, v in ((n.lower(), v) for n, v in pins.items()) if k in wanted}, wanted)
        self.assertIn("pip install --require-hashes --only-binary=:all: --no-deps -r requirements.txt",
                      (APP_DIR / "Dockerfile").read_text(encoding="utf-8"))
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"pip install --quiet --require-hashes -r hri_manager/requirements\.txt", ci)), 2)

    def test_the_supervisor_check_is_pinned(self):
        ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.assertRegex(ci["env"]["SUPERVISOR_SHA"], r"^[0-9a-f]{40}$")
        steps = ci["jobs"]["app"]["steps"]
        checkout = next(s for s in steps if s.get("with", {}).get("repository") == "home-assistant/supervisor")
        self.assertEqual(checkout["with"]["ref"], "${{ env.SUPERVISOR_SHA }}")
        check = next(s["run"] for s in steps if "app_supervisor_check.py" in s.get("run", ""))
        # PyYAML at the manager's own pin, taken from its lock: no version written in the workflow
        self.assertNotRegex(check, r"(?i)pyyaml==[0-9]")
        make = next(line for line in check.splitlines() if "constraints.txt" in line and "grep" in line)
        self.assertIn("hri_manager/requirements.txt", make)
        self.assertIn('-c "$RUNNER_TEMP/constraints.txt"', check)
        with tempfile.TemporaryDirectory(prefix="hri-mgr-test-") as tmp:
            proc = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", make], cwd=ROOT, capture_output=True, text=True,
                                  env={**os.environ, "RUNNER_TEMP": tmp})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(tmp, "constraints.txt"), encoding="utf-8") as fh:
                constraint = fh.read()
        lock = (APP_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertEqual(constraint, "pyyaml==" + re.search(r"(?m)^pyyaml==(\S+)", lock).group(1) + "\n")

    def test_the_dockerfile_base_is_pinned(self):
        self.assertRegex((APP_DIR / "Dockerfile").read_text(encoding="utf-8"), r"(?m)^FROM [\w./:-]+@sha256:[0-9a-f]{64}$")


class ScanLimitsDocTest(unittest.TestCase):
    def test_the_docs_state_the_search_limits(self):
        """F6: the limits of the search for decoys, as the code has them, in the README and DOCS.md."""
        limits = (f"{stamp.MAX_APP_CONFIG // (1024 * 1024)} MiB", f"{stamp.MAX_SCAN_DEPTH} folders deep",
                  f"{stamp.MAX_SCAN_ENTRIES:,} entries")
        for doc in (ROOT / "README.md", APP_DIR / "DOCS.md"):
            text = " ".join(doc.read_text(encoding="utf-8").split())
            for limit in limits:
                with self.subTest(doc=doc.name, limit=limit):
                    self.assertIn(limit, text)


class DevSmokeTest(unittest.TestCase):
    def test_an_interrupted_run_stops_after_its_cleanup(self):
        """tools/dev_smoke.sh's own trap lines: INT runs the cleanup and ends the script, which does not go on."""
        smoke = (ROOT / "tools" / "dev_smoke.sh").read_text(encoding="utf-8")
        traps = [line for line in smoke.splitlines() if line.startswith("trap ")]
        self.assertEqual(len(traps), 2, traps)
        script = "cleanup() { echo cleaned; }\n" + "\n".join(traps) + "\nkill -INT $$\necho went on\n"
        run = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 130, run)
        self.assertNotIn("went on", run.stdout)
        self.assertIn("cleaned", run.stdout)

    def test_the_playwright_image_and_package_are_pinned_together(self):
        smoke = (ROOT / "tools" / "dev_smoke.sh").read_text(encoding="utf-8")
        self.assertRegex(smoke, r"PLAYWRIGHT_IMAGE:-mcr\.microsoft\.com/playwright/python@sha256:[0-9a-f]{64}\}")
        self.assertRegex(smoke, r"PLAYWRIGHT_PIP:-1\.46\.0\}")


class RelativeUrlTest(unittest.TestCase):
    """Ingress serves the page under /api/hassio_ingress/<token>/: a URL starting with / leaves the app.  The one
    exception is panelHref() in mgr.js, which links Home Assistant's own pages (an instance's panel)."""

    def test_page_urls_are_relative(self):
        static = APP_DIR / "hrimgr" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'(?:href|src|action)="/', html), [])
        css = (static / "mgr.css").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"url\(", css), [])
        js = (static / "mgr.js").read_text(encoding="utf-8")
        body = re.sub(r"function panelHref\(i\) \{.*?\n\}\n", "", js, flags=re.S)
        self.assertNotEqual(body, js, "panelHref() not found")
        self.assertEqual(re.findall(r"""(?:fetch|get|send)\(\s*[`'"]/""", body), [])
        self.assertEqual(re.findall(r"""['"`]/(?!/)[a-z]""", body), [])
        self.assertEqual(re.findall(r"location\s*=", js), [])

    def test_no_redirect_to_an_absolute_path(self):
        for path in (APP_DIR / "hrimgr").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"HTTP(?:Found|SeeOther|MovedPermanently|TemporaryRedirect|PermanentRedirect)\(", src))

    def test_no_external_resources(self):
        static = APP_DIR / "hrimgr" / "static"
        for name in ("index.html", "mgr.css"):
            text = (static / name).read_text(encoding="utf-8")
            srcs = re.findall(r'(?:src|href)="(https?:[^"]+)"', text)
            links = [s for s in srcs if not s.startswith("https://github.com/trailro/")]
            self.assertEqual(links, [], name)
            self.assertNotIn("<link rel=\"stylesheet\" href=\"http", text)
            self.assertNotIn("@import", text)


class PublicRepoTest(unittest.TestCase):
    """No private infrastructure: addresses other than the Supervisor's documented one, loopback and the documentation
    ranges; no home folders.  The operator's own names live in CLAUDE.local.md, which is gitignored."""

    ALLOWED_IP = re.compile(r"^(?:172\.30\.32\.2|127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+)$")

    def test_no_private_addresses_or_paths(self):
        problems = []
        for path in repo_files():
            if path.suffix in (".png", ".ico", ".gz") or path.name == "LICENSE":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(ROOT).as_posix()
            for ip in re.findall(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])", text):
                if not self.ALLOWED_IP.match(ip) and all(int(p) <= 255 for p in ip.split(".")):
                    problems.append(f"{rel}: {ip}")
            # home folders and tailnet names; written in two pieces so this file does not match itself
            for pattern in ("/" + "Users/", "/" + "home/[a-z]", r"\.ts" + r"\.net\b"):
                if re.search(pattern, text, re.I):
                    problems.append(f"{rel}: {pattern}")
        self.assertEqual(problems, [])

    def test_actions_are_pinned_by_sha(self):
        for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
            for line in wf.read_text(encoding="utf-8").splitlines():
                m = re.search(r"uses:\s*([^\s#]+)", line)
                if m and not m.group(1).startswith("./"):
                    with self.subTest(workflow=wf.name, uses=m.group(1)):
                        self.assertRegex(m.group(1), r"@[0-9a-f]{40}$")

    def test_gitignore(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in ("CLAUDE.local.md", ".env", "._*", "__pycache__/"):
            self.assertIn(entry, ignored)


if __name__ == "__main__":
    unittest.main()
