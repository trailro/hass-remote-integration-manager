"""The repository as the Supervisor and a reader see it: one app, its config pinned, versions in step, relative URLs
only, development mode unreachable from the app, and nothing private."""

import pathlib
import re
import subprocess
import unittest

import yaml

from hrimgr import VERSION, names, settings, stamp

from . import APP_DIR, ROOT

CONFIG = yaml.safe_load((APP_DIR / "config.yaml").read_text(encoding="utf-8"))


def repo_files() -> list[pathlib.Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]


class AppConfigTest(unittest.TestCase):
    def test_the_store_finds_one_app(self):
        self.assertEqual(stamp.find_configs(str(ROOT)), ["hri_manager/config.yaml"])

    def test_config(self):
        self.assertEqual(CONFIG["slug"], "hri_manager")
        # the version, and the image: line, are the Image workflow's app-version job's to write, after the image of
        # that version is pushed (tests/test_image_workflow.py): never ahead of the code, and only this image
        self.assertLessEqual(names.parse_version(CONFIG["version"]), names.parse_version(VERSION))
        self.assertIn(CONFIG.get("image"), (None, "ghcr.io/trailro/hass-remote-integration-manager"))
        self.assertEqual((CONFIG["hassio_api"], CONFIG["hassio_role"]), (True, "manager"))
        self.assertEqual(CONFIG["map"], [{"type": "local_apps", "read_only": False}])
        self.assertTrue(CONFIG["ingress"])
        self.assertNotIn("ports", CONFIG)
        self.assertNotIn("webui", CONFIG)
        self.assertEqual(CONFIG["stage"], "experimental")
        self.assertEqual(sorted(CONFIG["arch"]), ["aarch64", "amd64"])
        self.assertIs(CONFIG["homeassistant_api"], True)  # Core's config/auth/list: administrators only (corews.py)
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

    def test_the_dockerfile_base_is_pinned(self):
        self.assertRegex((APP_DIR / "Dockerfile").read_text(encoding="utf-8"), r"(?m)^FROM [\w./:-]+@sha256:[0-9a-f]{64}$")


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
