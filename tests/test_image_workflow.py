"""The Image workflow (.github/workflows/image.yml): the manager's image for amd64 and arm64 on ghcr.io, and the
app-version job that moves hri_manager/config.yaml's version, and adds its image: line, only after that image is
pushed and anyone can pull it.  The store must never offer a version whose image does not exist.

AppVersionStepTest runs the workflow's own script (not a copy) on a scratch repository with a remote, and a `gh` that
answers with a list of releases, as hass-remote-integration's tests/test_ha_app.py does for its app."""

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml

from hrimgr import VERSION, names

from . import APP_DIR, ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "image.yml"
APP_CONFIG = APP_DIR / "config.yaml"
IMAGE = "ghcr.io/trailro/hass-remote-integration-manager"
GH = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:3] != ["release", "list"]:
    sys.exit(f"gh stub: no answer for {sys.argv[1:]}")
print(os.environ["STUB_RELEASES"])
"""


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(job: dict, name: str) -> str:
    return next(s["run"] for s in job["steps"] if s.get("name") == name)


class ImageJobTest(unittest.TestCase):
    def test_multi_arch_to_ghcr_from_the_app_folder(self):
        wf = _workflow()
        self.assertEqual(wf[True]["release"]["types"], ["published", "released"])  # yaml reads "on" as True
        job = wf["jobs"]["image"]
        self.assertEqual(job["permissions"], {"contents": "read", "packages": "write"})
        build = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/build-push-action@"))
        self.assertEqual(build["with"]["context"], "hri_manager")
        self.assertEqual(build["with"]["platforms"], "linux/amd64,linux/arm64")
        meta = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/metadata-action@"))
        self.assertEqual(meta["with"]["images"], "ghcr.io/${{ github.repository }}")
        self.assertEqual(wf["jobs"]["app-version"]["env"]["IMAGE"], "ghcr.io/${{ github.repository }}")
        self.assertIn("push: true", WORKFLOW.read_text(encoding="utf-8"))
        # no other workflow pushes an image
        for other in (ROOT / ".github" / "workflows").glob("*.yml"):
            if other != WORKFLOW:
                self.assertNotIn("push: true", other.read_text(encoding="utf-8"), other.name)

    def test_the_tag_s_code_must_be_that_version(self):
        script = _step(_workflow()["jobs"]["image"], "The tag's code is that version")
        for tag, rc in ((f"v{VERSION}", 0), ("v9.9.9", 1)):
            with self.subTest(tag=tag):
                proc = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=ROOT, capture_output=True,
                                      text=True, env={**os.environ, "TAG": tag})
                self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)

    def test_the_version_moves_only_after_an_anonymous_pull_works(self):
        job = _workflow()["jobs"]["app-version"]
        self.assertEqual(job["needs"], "image")
        self.assertEqual(job["if"], "needs.image.result == 'success'")
        self.assertEqual(job["permissions"], {"contents": "write"})
        steps = [s.get("name") or s.get("uses") for s in job["steps"]]
        self.assertLess(steps.index("Anyone can pull the image, for both architectures"),
                        steps.index("Set hri_manager/config.yaml version and image"))
        pull = _step(job, "Anyone can pull the image, for both architectures")
        self.assertIn('DOCKER_CONFIG="$(mktemp -d)"', pull)  # no credentials: as a Supervisor pulls
        self.assertIn("linux/arm64", pull)
        self.assertIn("linux/amd64", pull)


class AppVersionStepTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None or shutil.which("bash") is None:
            self.skipTest("needs git and bash")
        self.script = _step(_workflow()["jobs"]["app-version"], "Set hri_manager/config.yaml version and image")
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="hri-mgr-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.remote, self.work, self.bin = tmp / "remote.git", tmp / "work", tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(GH, encoding="utf-8")
        (self.bin / "gh").chmod(stat.S_IRWXU)
        # python3 as the tests run it: the script's own python3 calls need nothing but the standard library
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                    "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"}
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(k, None)
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote))
        self.git("clone", "-q", str(self.remote), str(self.work))
        (self.work / "hri_manager").mkdir()
        # main as a release leaves it: the previous version and, before the first image, no image: line
        self.original = re.sub(r"(?m)^image: .*\n", "", APP_CONFIG.read_text(encoding="utf-8"))
        self.original = re.sub(r'(?m)^version: .*$', 'version: "0.1.0"', self.original)
        self.config_path = self.work / "hri_manager" / "config.yaml"
        self.config_path.write_text(self.original, encoding="utf-8")
        self.commit("main before the release")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True).stdout

    def commit(self, message):
        self.git("add", "-A", cwd=self.work)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message, cwd=self.work)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.work)

    def run_step(self, tag, releases=(("v0.1.0", False), ("v0.1.1", False), ("v0.2.0b1", True))):
        env = {**self.env, "TAG": tag, "BRANCH": "main", "IMAGE": IMAGE,
               "GITHUB_REPOSITORY": "trailro/hass-remote-integration-manager",
               "STUB_RELEASES": json.dumps([{"tagName": t, "isPrerelease": pre, "isDraft": False} for t, pre in releases])}
        proc = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", self.script], cwd=self.work, env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = self.git("--git-dir", str(self.remote), "show", "main:hri_manager/config.yaml")
        return yaml.safe_load(text), text, proc.stdout

    def log(self):
        return self.git("--git-dir", str(self.remote), "log", "--format=%s", "main").splitlines()

    def test_the_newest_stable_release_sets_version_and_image_once(self):
        config, _, _ = self.run_step("v0.1.0")  # not the newest: nothing moves
        self.assertEqual((config["version"], config.get("image")), ("0.1.0", None))
        config, _, _ = self.run_step("v0.1.1", releases=(("v0.1.0", False), ("v0.1.1", True)))  # a pre-release
        self.assertEqual((config["version"], config.get("image")), ("0.1.0", None))
        config, text, _ = self.run_step("v0.1.1")
        self.assertEqual((config["version"], config["image"]), ("0.1.1", IMAGE))
        self.assertIn(f'version: "0.1.1"\nimage: {IMAGE}\n', text)
        self.assertEqual(self.log()[0], "hri_manager: version 0.1.1, the image of v0.1.1 is published")
        rest = {k: v for k, v in config.items() if k not in ("version", "image")}
        original = {k: v for k, v in yaml.safe_load(self.original).items() if k != "version"}
        self.assertEqual(rest, original)  # only the version line changed, and the image line was added
        commits = len(self.log())
        config, _, out = self.run_step("v0.1.1")  # a manual re-run: nothing to commit
        self.assertIn("already names 0.1.1 and its image", out)
        self.assertEqual(len(self.log()), commits)
        config, text, _ = self.run_step("v0.1.2", releases=(("v0.1.1", False), ("v0.1.2", False)))
        self.assertEqual((config["version"], config["image"]), ("0.1.2", IMAGE))
        self.assertEqual(len(re.findall(r"(?m)^image: ", text)), 1)
        self.assertTrue(names.parse_version(config["version"]))

    def test_a_stable_patch_after_a_newer_prerelease_moves_the_version(self):
        releases = (("v0.1.0", False), ("v0.1.1", False), ("v0.2.0", True))
        self.assertEqual(self.run_step("v0.2.0", releases=releases)[0]["version"], "0.1.0")
        self.assertEqual(self.run_step("v0.1.1", releases=releases)[0]["version"], "0.1.1")

    def test_the_version_never_moves_back(self):
        self.config_path.write_text(self.original.replace('version: "0.1.0"', 'version: "0.3.0"'), encoding="utf-8")
        self.commit("a newer version by hand")
        config, _, out = self.run_step("v0.1.1")
        self.assertEqual((config["version"], config.get("image")), ("0.3.0", None))
        self.assertIn("newer than 0.1.1", out)

    def test_versions_compare_as_numbers(self):
        """0.10.0 is newer than 0.9.0 (a text sort says otherwise)."""
        releases = (("v0.9.0", False), ("v0.10.0", False))
        self.assertEqual(self.run_step("v0.9.0", releases=releases)[0]["version"], "0.1.0")
        self.assertEqual(self.run_step("v0.10.0", releases=releases)[0]["version"], "0.10.0")


if __name__ == "__main__":
    unittest.main()
