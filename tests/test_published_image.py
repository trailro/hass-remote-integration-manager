"""The anonymous-pull check (.github/check_published_image.py) that CI runs on every change and the Image workflow runs
before it moves the app version: the image hri_manager/config.yaml names, at its version, must be pullable without
credentials for amd64 and arm64.  A hand edit that makes main name a missing or private image fails CI; before the
first release there is no image: line, and the check passes.  A `docker` on PATH answers as the registry would."""

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml

from . import APP_DIR, ROOT

SCRIPT = ROOT / ".github" / "check_published_image.py"
IMAGE = "ghcr.io/trailro/hass-remote-integration-manager"
DOCKER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as fh:
    config = os.environ.get("DOCKER_CONFIG")
    fh.write(json.dumps({"argv": sys.argv[1:], "anonymous": bool(config) and os.listdir(config) == []}) + "\\n")
if sys.argv[1:3] != ["manifest", "inspect"] or not os.environ.get("FAKE_MANIFEST"):
    sys.exit("no such manifest: unauthorized")
print(os.environ["FAKE_MANIFEST"])
"""


def index(*platforms):
    return json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                       "manifests": [{"digest": "sha256:" + "0" * 64, "platform": {"os": o, "architecture": a}}
                                     for o, a in (p.split("/") for p in platforms)]})


class PublishedImageTest(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="hri-mgr-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "bin").mkdir()
        (tmp / "bin" / "docker").write_text(DOCKER, encoding="utf-8")
        (tmp / "bin" / "docker").chmod(stat.S_IRWXU)
        self.tmp, self.log = tmp, tmp / "docker.log"
        self.log.write_text("", encoding="utf-8")
        self.config = (APP_DIR / "config.yaml").read_text(encoding="utf-8")

    def run_check(self, config_text, manifest=None):
        path = self.tmp / "config.yaml"
        path.write_text(config_text, encoding="utf-8")
        env = {**os.environ, "PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
               "FAKE_LOG": str(self.log), "FAKE_MANIFEST": manifest or ""}
        proc = subprocess.run([sys.executable, str(SCRIPT), "--config", str(path)], env=env, capture_output=True, text=True)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.log.write_text("", encoding="utf-8")
        return proc, calls

    def with_image(self, version="0.1.1"):
        text = self.config.replace(f'version: "{yaml.safe_load(self.config)["version"]}"',
                                   f'version: "{version}"\nimage: {IMAGE}')
        self.assertIn(f"image: {IMAGE}", text)
        return text

    def test_no_image_line_passes_without_asking(self):
        proc, calls = self.run_check(self.config.replace(f"image: {IMAGE}\n", ""))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, [])
        self.assertIn("no image", proc.stdout)

    def test_a_pullable_image_for_both_architectures_passes(self):
        proc, calls = self.run_check(self.with_image(), index("linux/amd64", "linux/arm64", "unknown/unknown"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(calls, [{"argv": ["manifest", "inspect", f"{IMAGE}:0.1.1"], "anonymous": True}])

    def test_a_missing_or_private_or_partial_image_fails(self):
        for label, manifest in (("missing or private", None), ("no arm64", index("linux/amd64")),
                                ("no amd64", index("linux/arm64")),
                                ("one platform, no index", json.dumps({"schemaVersion": 2, "config": {}}))):
            with self.subTest(label):
                proc, calls = self.run_check(self.with_image(), manifest)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertTrue(calls and calls[0]["anonymous"])

    def test_ci_and_the_image_workflow_run_it(self):
        ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        runs = [s.get("run", "") for job in ci["jobs"].values() for s in job["steps"]]
        self.assertTrue(any("python3 .github/check_published_image.py --config hri_manager/config.yaml" in r for r in runs))
        image = yaml.safe_load((ROOT / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8"))
        pull = next(s for s in image["jobs"]["app-version"]["steps"]
                    if s.get("name") == "Anyone can pull the image, for both architectures")
        self.assertIn('python3 .github/check_published_image.py --ref "$IMAGE:${TAG#v}"', pull["run"])


if __name__ == "__main__":
    unittest.main()
