"""The Image workflow (.github/workflows/image.yml): the manager's image for amd64 and arm64 on ghcr.io, and the
app-version job that moves hri_manager/config.yaml's version, and adds its image: line, only after that image is
pushed and anyone can pull it.  The store must never offer a version whose image does not exist.

AppVersionStepTest runs the workflow's own script (not a copy) on a scratch repository with a remote, and a `gh` that
answers with a list of releases, as hass-remote-integration's tests/test_ha_app.py does for its app."""

import importlib.util
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
        checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        # the tag by its full name: actions/checkout looks a bare name up as a branch first
        self.assertEqual(checkout["with"]["ref"], "refs/tags/${{ env.TAG }}")
        self.assertIs(checkout["with"]["persist-credentials"], False)
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
        self.assertEqual(job["permissions"], {"contents": "write", "packages": "write"})
        steps = [s.get("name") or s.get("uses") for s in job["steps"]]
        # decide first; the pull check and the write only when the version really moves (a pre-release, or a manual
        # re-run of an old tag, with a still private package must not fail)
        self.assertLess(steps.index("Decide whether the app version moves"),
                        steps.index("Anyone can pull the image, for both architectures"))
        self.assertLess(steps.index("Anyone can pull the image, for both architectures"),
                        steps.index("Set hri_manager/config.yaml version and image"))
        decide = next(s for s in job["steps"] if s.get("name") == "Decide whether the app version moves")
        self.assertNotIn("if", decide)
        for name in ("Anyone can pull the image, for both architectures", "Set hri_manager/config.yaml version and image"):
            step = next(s for s in job["steps"] if s.get("name") == name)
            self.assertEqual(step.get("if"), f"steps.{decide['id']}.outputs.move == 'true'", name)
        pull = _step(job, "Anyone can pull the image, for both architectures")
        # anonymously, both architectures: tests/test_published_image.py tests that script
        self.assertIn('.github/check_published_image.py --ref "$IMAGE:${TAG#v}"', pull)
        # the write token is not kept in .git/config for every step: only the pull and push of the write get it
        checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        self.assertIs(checkout["with"]["persist-credentials"], False)
        write = next(s for s in job["steps"] if s.get("name") == "Set hri_manager/config.yaml version and image")
        self.assertEqual(write["env"], {"GH_TOKEN": "${{ github.token }}"})
        authed = [line for line in write["run"].splitlines() if "GH_TOKEN" in line or "extraheader" in line]
        self.assertEqual(len(authed), 3, authed)  # the header built once, then the pull and the push
        self.assertNotRegex(write["run"], r"https://[^ ]*\$GH_TOKEN|x-access-token:\$")


class ImagePromotionStepTest(unittest.TestCase):
    """Run the locked job's own decision/promotion scripts; docker is a command-recording stub."""

    def setUp(self):
        self.job = _workflow()["jobs"]["app-version"]
        self.decide = _step(self.job, "Is this the newest stable release, and the newest of its X.Y series?")
        self.promote = _step(self.job, "Promote the built digest to shared tags")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="hri-mgr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, script in {"gh": GH, "docker": """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_DOCKER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
"""}.items():
            path = self.bin / name
            path.write_text(script, encoding="utf-8")
            path.chmod(stat.S_IRWXU)
        (self.bin / "python3").symlink_to(sys.executable)
        self.output = self.tmp / "output"
        self.log = self.tmp / "docker_log"
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "GITHUB_REPOSITORY": "trailro/hass-remote-integration-manager", "IMAGE": IMAGE,
                    "GITHUB_OUTPUT": str(self.output), "STUB_DOCKER_LOG": str(self.log)}

    def run_promotion(self, tag, releases, event="release", digest="sha256:" + "a" * 64):
        self.output.write_text("", encoding="utf-8")
        env = {**self.env, "TAG": tag, "DIGEST": digest, "GITHUB_EVENT_NAME": event,
               "STUB_RELEASES": json.dumps(releases)}
        for script in (self.decide, self.promote):
            if script == self.promote:
                outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
                env.update(LATEST=outputs["enable"], MINOR=outputs["minor"])
            run = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    @staticmethod
    def releases(*tags):
        return [{"tagName": tag, "isPrerelease": False, "isDraft": False} for tag in tags]

    def test_only_exact_version_is_built_and_all_promotions_share_a_queue(self):
        wf = _workflow()
        image = wf["jobs"]["image"]
        meta = next(step for step in image["steps"] if step.get("id") == "meta")
        self.assertEqual(meta["with"]["flavor"], "latest=false")
        self.assertEqual(meta["with"]["tags"].strip(), "type=semver,pattern={{version}},value=${{ env.TAG }}")
        self.assertEqual(image["outputs"]["digest"], "${{ steps.build.outputs.digest }}")
        self.assertEqual(self.job["env"]["DIGEST"], "${{ needs.image.outputs.digest }}")
        self.assertEqual(self.job["concurrency"],
                         {"group": "image-promotion", "cancel-in-progress": False, "queue": "max"})
        self.assertIn("inputs.tag", wf["concurrency"]["group"])
        self.assertEqual(wf["concurrency"]["queue"], "max")
        self.assertNotIn("latest", [step.get("id") for step in image["steps"]])
        names = [step.get("name") for step in self.job["steps"]]
        self.assertLess(names.index("Is this the newest stable release, and the newest of its X.Y series?"),
                        names.index("Promote the built digest to shared tags"))
        self.assertLess(names.index("Promote the built digest to shared tags"),
                        names.index("Decide whether the app version moves"))

    def test_old_build_finishing_after_new_promotion_cannot_lower_shared_tags(self):
        releases = self.releases("v0.2.1", "v0.2.2")
        newest_digest = "sha256:" + "b" * 64
        expected = [["buildx", "imagetools", "create", "--tag", f"{IMAGE}:latest", "--tag", f"{IMAGE}:0.2",
                     f"{IMAGE}@{newest_digest}"]]
        self.assertEqual(self.run_promotion("v0.2.2", releases, digest=newest_digest), expected)
        self.assertEqual(self.run_promotion("v0.2.1", releases), expected)

    def test_each_series_is_promoted_independently_and_versions_compare_numerically(self):
        releases = self.releases("v0.9.0", "v0.9.1", "v0.10.0")
        self.assertEqual(self.run_promotion("v0.9.0", releases), [])
        commands = self.run_promotion("v0.9.1", releases)
        self.assertEqual(commands[0][3:5], ["--tag", f"{IMAGE}:0.9"])
        self.assertNotIn(f"{IMAGE}:latest", commands[0])
        commands = self.run_promotion("v0.10.0", releases)
        self.assertIn(f"{IMAGE}:latest", commands[-1])
        self.assertIn(f"{IMAGE}:0.10", commands[-1])

    def test_manual_runs_never_move_latest_and_prereleases_and_drafts_move_nothing(self):
        releases = self.releases("v0.2.1") + [
            {"tagName": "v0.2.2", "isPrerelease": True, "isDraft": False},
            {"tagName": "v0.3.0", "isPrerelease": False, "isDraft": True}]
        for tag in ("v0.2.2", "v0.3.0", "v0.2.3-rc1"):
            self.assertEqual(self.run_promotion(tag, releases), [])
        command = self.run_promotion("v0.2.1", releases, event="workflow_dispatch")[0]
        self.assertEqual(command[3:5], ["--tag", f"{IMAGE}:0.2"])
        self.assertNotIn(f"{IMAGE}:latest", command)


class AppVersionScriptTest(unittest.TestCase):
    def test_versions_are_ascii_digits(self):
        spec = importlib.util.spec_from_file_location("app_version", ROOT / ".github" / "app_version.py")
        app_version = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app_version)
        self.assertEqual(app_version.key("0.10.2"), (0, 10, 2))
        with self.assertRaises(ValueError):
            app_version.key("٠.١٠.٢")
        self.assertEqual(app_version.newest_stable([{"tagName": "v٩.٠.٠"}, {"tagName": "v0.1.1"}]), "v0.1.1")


class AppVersionStepTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None or shutil.which("bash") is None:
            self.skipTest("needs git and bash")
        job = _workflow()["jobs"]["app-version"]
        self.decide = _step(job, "Decide whether the app version moves")
        self.script = _step(job, "Set hri_manager/config.yaml version and image")
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
        (self.work / ".github").mkdir()
        shutil.copy(ROOT / ".github" / "app_version.py", self.work / ".github" / "app_version.py")
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

    def run_step(self, tag, releases=(("v0.1.0", False), ("v0.1.1", False), ("v0.2.0-rc1", True))):
        """The job as GitHub runs it: the decision, then (only when it says so) the pull check and the write."""
        output = self.work.parent / "github_output"
        output.write_text("", encoding="utf-8")
        env = {**self.env, "TAG": tag, "BRANCH": "main", "IMAGE": IMAGE, "GITHUB_OUTPUT": str(output),
               "GITHUB_REPOSITORY": "trailro/hass-remote-integration-manager",
               "STUB_RELEASES": json.dumps([{"tagName": t, "isPrerelease": pre, "isDraft": False} for t, pre in releases])}
        out = ""
        for script in (self.decide, self.script):
            if script is self.script and "move=true" not in output.read_text(encoding="utf-8").split():
                break
            proc = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=self.work, env=env,
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out += proc.stdout
        self.moved = "move=true" in output.read_text(encoding="utf-8").split()
        text = self.git("--git-dir", str(self.remote), "show", "main:hri_manager/config.yaml")
        return yaml.safe_load(text), text, out

    def log(self):
        return self.git("--git-dir", str(self.remote), "log", "--format=%s", "main").splitlines()

    def test_the_newest_stable_release_sets_version_and_image_once(self):
        config, _, _ = self.run_step("v0.1.0")  # not the newest: nothing moves, nothing is checked
        self.assertEqual((config["version"], config.get("image"), self.moved), ("0.1.0", None, False))
        config, _, _ = self.run_step("v0.1.1", releases=(("v0.1.0", False), ("v0.1.1", True)))  # a pre-release
        self.assertEqual((config["version"], config.get("image"), self.moved), ("0.1.0", None, False))
        config, text, _ = self.run_step("v0.1.1")
        self.assertTrue(self.moved)
        self.assertEqual((config["version"], config["image"]), ("0.1.1", IMAGE))
        self.assertIn(f'version: "0.1.1"\nimage: {IMAGE}\n', text)
        self.assertEqual(self.log()[0], "hri_manager: version 0.1.1, the image of v0.1.1 is published")
        rest = {k: v for k, v in config.items() if k not in ("version", "image")}
        original = {k: v for k, v in yaml.safe_load(self.original).items() if k != "version"}
        self.assertEqual(rest, original)  # only the version line changed, and the image line was added
        commits = len(self.log())
        config, _, out = self.run_step("v0.1.1")  # a manual re-run: nothing to commit, no pull check
        self.assertIn("already names 0.1.1 and its image", out)
        self.assertFalse(self.moved)
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
        self.assertEqual((config["version"], config.get("image"), self.moved), ("0.3.0", None, False))
        self.assertIn("newer than 0.1.1", out)

    def test_a_version_it_cannot_read_fails_clearly(self):
        """Never a guess: a version that is not X.Y.Z, or no version line, stops the job with the reason."""
        output = self.work.parent / "github_output"
        releases = json.dumps([{"tagName": "v0.1.1", "isPrerelease": False, "isDraft": False}])
        env = {**self.env, "TAG": "v0.1.1", "BRANCH": "main", "IMAGE": IMAGE, "GITHUB_OUTPUT": str(output),
               "GITHUB_REPOSITORY": "trailro/hass-remote-integration-manager", "STUB_RELEASES": releases}
        for text, why in ((self.original.replace('version: "0.1.0"', 'version: "0.1.0-dev"'), "is not a stable version X.Y.Z"),
                          (self.original.replace('version: "0.1.0"\n', ""), "has no version: line")):
            with self.subTest(why=why):
                self.config_path.write_text(text, encoding="utf-8")
                output.write_text("", encoding="utf-8")
                proc = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", self.decide], cwd=self.work, env=env,
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn(why, proc.stderr)
                self.assertNotIn("move=true", output.read_text(encoding="utf-8"))

    def test_versions_compare_as_numbers(self):
        """0.10.0 is newer than 0.9.0 (a text sort says otherwise)."""
        releases = (("v0.9.0", False), ("v0.10.0", False))
        self.assertEqual(self.run_step("v0.9.0", releases=releases)[0]["version"], "0.1.0")
        self.assertEqual(self.run_step("v0.10.0", releases=releases)[0]["version"], "0.10.0")


if __name__ == "__main__":
    unittest.main()
