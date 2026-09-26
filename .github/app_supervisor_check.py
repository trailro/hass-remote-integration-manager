"""The manager and a stamped instance, read by the Supervisor's own code instead of a copy of its schema.

    python .github/app_supervisor_check.py <supervisor checkout> [<repository root>]

The linter CI runs (frenck/action-app-linter) follows a JSON schema kept by hand.  This imports the Supervisor from a
checkout of home-assistant/supervisor (its requirements installed; the package itself never: "supervisor" on PyPI is
supervisord) and runs what the Supervisor runs when it reads an app:

  - discovery: the config.* files its store finds in this repository are exactly hri_manager/config.yaml;
  - SCHEMA_APP_CONFIG on hri_manager/config.yaml, SCHEMA_APP_TRANSLATIONS on its translations, AppOptions on its
    default options.  Any warning logged fails, and so does a key the schema drops (it drops unknown keys silently);
  - the same for an instance stamped (hrimgr.stamp) from HRI's app template, the fixture of v0.25.0, on both
    channels (a release keeps the image, a git build has none and a 0.0.0-<sha> version);
  - App._is_excluded_by_filter over the instance's folder as the Supervisor names it (local_hri_<name>): every path
    HRI's own backup_exclude leaves out of its folder (<repo>_hass_remote_integration) is left out of the
    instance's, and what HRI keeps is kept;
  - the same filter over three release instances side by side (app_configs/local_hri_<name>/ each, as on one
    machine): none of them keeps a venv, HRI's own backups or a log, and each keeps its state files.

Every section runs and prints its problems; the exit status is 1 when any section failed."""

import copy
import logging
import pathlib
import re
import sys
from pathlib import PurePath
from types import SimpleNamespace

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURE = ROOT / "tests" / "fixtures" / "hri_v0.25.0"
CONFIG_PARENT = PurePath("/data/app_configs")
HRI_FOLDER = CONFIG_PARENT / "5c53de3b_hass_remote_integration"
MANAGER_IMAGE = "ghcr.io/trailro/hass-remote-integration-manager"
# HRI's live state, which a backup must keep (relative to its folder)
# what no instance's backup may hold (relative to its folder): the installed Home Assistant (about 800 MB), HRI's
# own backups, logs
DISPOSABLE = ("venv-current", "venv-current/lib/python3.14/site-packages/homeassistant/__init__.py",
              "venv-2026.9.3/bin/python", "backups/hri-backup-20260926.zip", "integration_manager/backups/pre-update.zip",
              "home-assistant.log", "home-assistant.log.1", "integration_manager/process.log",
              "integration_manager/ha-install.log", ".storage/core.restore_state.log", "deps/lib/x.py")
INSTANCES = ("garage", "lab", "boiler_room")
STATE_FILES = ("configuration.yaml", ".storage/core.config_entries", ".storage/core.device_registry",
               "integration_manager/settings.json", "integration_manager/state.json", "integration_manager/mqtt.json",
               "custom_components/ramses_cc/manifest.json", "integration_manager/versions/x/1.0/custom_components/x/__init__.py")


class Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def dropped_keys(given, kept, path: str = "") -> list[str]:
    out = []
    if isinstance(given, dict) and isinstance(kept, dict):
        for key, value in given.items():
            where = f"{path}.{key}" if path else str(key)
            if key not in kept:
                out.append(where)
            else:
                out += dropped_keys(value, kept[key], where)
    elif isinstance(given, list) and isinstance(kept, list) and len(given) == len(kept):
        for i, (a, b) in enumerate(zip(given, kept)):
            out += dropped_keys(a, b, f"{path}[{i}]")
    return out


def example_paths(glob: str) -> list[str]:
    """Paths ``glob`` matches, relative to the app's folder: character classes become their first character, * a
    name and, where it can, nothing."""
    base = re.sub(r"\[(.)[^\]]*\]", r"\1", glob)
    return sorted({base.replace("*", "x1"), base.replace("*", "")} - {""})


class Check:
    def __init__(self, supervisor: pathlib.Path, root: pathlib.Path):
        sys.path.insert(0, str(supervisor))
        sys.path.insert(0, str(root / "hri_manager"))
        self.root = root
        self.failed = False
        self.handler = Warnings()
        logging.getLogger().addHandler(self.handler)
        logging.getLogger().setLevel(logging.WARNING)

    def section(self, title: str, problems: list[str]) -> None:
        if problems:
            self.failed = True
            print(f"FAIL  {title}")
            for p in problems:
                print(f"      - {p}")
        else:
            print(f"ok    {title}")

    def validate(self, title: str, raw: dict):
        import voluptuous as vol
        from supervisor.apps.validate import SCHEMA_APP_CONFIG

        start = len(self.handler.records)
        problems = []
        try:
            config = SCHEMA_APP_CONFIG(copy.deepcopy(raw))
        except vol.Invalid as err:
            config = None
            problems.append(f"invalid: {err}")
        problems += [f"warning: {m}" for m in self.handler.records[start:]]
        if config is not None:
            problems += [f"dropped by the schema: {k}" for k in dropped_keys(raw, config)]
        self.section(title, problems)
        return config

    def options(self, title: str, config: dict) -> None:
        import voluptuous as vol
        from supervisor.apps.options import AppOptions

        start = len(self.handler.records)
        problems = []
        try:
            AppOptions(None, config["schema"], config["name"], config["slug"])(config["options"])
        except vol.Invalid as err:
            problems.append(f"invalid: {err}")
        problems += [f"warning: {m}" for m in self.handler.records[start:]]
        self.section(title, problems)

    def translations(self, title: str, raw: dict, config: dict) -> None:
        import voluptuous as vol
        from supervisor.apps.validate import SCHEMA_APP_TRANSLATIONS

        start = len(self.handler.records)
        problems = []
        try:
            problems += [f"dropped by the schema: {k}" for k in dropped_keys(raw, SCHEMA_APP_TRANSLATIONS(copy.deepcopy(raw)))]
        except vol.Invalid as err:
            problems.append(f"invalid: {err}")
        unknown = set(raw.get("configuration") or {}) - set(config.get("schema") or {})
        problems += [f"translates an option the app does not have: {k}" for k in sorted(unknown)]
        problems += [f"warning: {m}" for m in self.handler.records[start:]]
        self.section(title, problems)

    def backup_filter(self, title: str, original: dict, stamped: dict, slug: str) -> None:
        from supervisor.apps.app import App

        def excluded(config: dict, folder: PurePath, rel: str) -> bool:
            app = SimpleNamespace(backup_exclude=config.get("backup_exclude") or [])
            parts = PurePath(rel).parts
            return any(App._is_excluded_by_filter(app, folder, "config", PurePath("config", *parts[:i]))
                       for i in range(1, len(parts) + 1))

        folder = CONFIG_PARENT / slug
        samples = set(STATE_FILES)
        for glob in original.get("backup_exclude") or []:
            for path in example_paths(glob):
                rel = path.split("/", 1)[1] if path.startswith("x1_hass_remote_integration/") or path.startswith("_hass_remote_integration/") else path
                samples.add(rel.lstrip("/"))
        problems = []
        out = 0
        for rel in sorted(s for s in samples if s):
            before = excluded(original, HRI_FOLDER, rel)
            after = excluded(stamped, folder, rel)
            out += before
            if before != after:
                problems.append(f"{rel}: HRI {'leaves it out' if before else 'keeps it'}, the instance {'leaves it out' if after else 'keeps it'}")
        problems += [f"live state left out: {rel}" for rel in STATE_FILES if excluded(stamped, folder, rel)]
        if out < 10:
            problems.append(f"only {out} sample paths are left out by HRI's own filter: the samples are wrong")
        self.section(f"{title} ({len(samples)} paths, {out} left out)", problems)

    def instances_side_by_side(self, template: dict) -> None:
        from supervisor.apps.app import App

        from hrimgr import stamp

        import yaml

        stamped = {name: yaml.safe_load(stamp.dump(stamp.stamp(template, name, "0.25.0", "release"), "check"))
                   for name in INSTANCES}
        problems, left_out = [], 0
        for name, config in stamped.items():
            folder = CONFIG_PARENT / f"local_hri_{name}"
            app = SimpleNamespace(backup_exclude=config.get("backup_exclude") or [])

            def excluded(rel: str) -> bool:
                parts = PurePath(rel).parts
                return any(App._is_excluded_by_filter(app, folder, "config", PurePath("config", *parts[:i]))
                           for i in range(1, len(parts) + 1))

            for rel in DISPOSABLE:
                if excluded(rel):
                    left_out += 1
                else:
                    problems.append(f"local_hri_{name}/{rel} is kept in the instance's backup")
            problems += [f"local_hri_{name}/{rel}: live state left out" for rel in STATE_FILES if excluded(rel)]
        self.section(f"{len(INSTANCES)} release instances side by side: no venv, HRI backups or logs kept, state kept "
                     f"({left_out} paths left out)", problems)

    def run(self) -> int:
        from supervisor.const import FILE_SUFFIX_CONFIGURATION
        from supervisor.utils.common import read_json_or_yaml_file

        from hrimgr import names, stamp

        found = sorted(p.relative_to(self.root).as_posix() for p in self.root.glob("**/config.*")
                       if not [part for part in p.relative_to(self.root).parts if part.startswith(".") or part == "rootfs"]
                       and p.suffix in FILE_SUFFIX_CONFIGURATION)
        self.section("the Supervisor finds one app, hri_manager/", [] if found == ["hri_manager/config.yaml"] else [f"found {found}"])

        raw = read_json_or_yaml_file(self.root / "hri_manager" / "config.yaml")
        config = self.validate("hri_manager/config.yaml against SCHEMA_APP_CONFIG", raw)
        if config is None:
            return 1
        for path in sorted((self.root / "hri_manager" / "translations").glob("*")):
            if path.suffix in FILE_SUFFIX_CONFIGURATION:
                self.translations(f"hri_manager/translations/{path.name}", read_json_or_yaml_file(path), config)
        self.options("hri_manager default options (AppOptions)", config)
        # as the Image workflow's app-version job leaves it once the first image is published
        with_image = self.validate("hri_manager/config.yaml with the image: line of the app-version job",
                                   {**raw, "image": MANAGER_IMAGE})
        if with_image is not None:
            self.section("with the image line, the Supervisor pulls instead of building",
                         [] if with_image.get("image") == MANAGER_IMAGE else [f"image {with_image.get('image')!r}"])

        template = stamp.parse_template((FIXTURE / "app_config.yaml").read_bytes())
        tr = read_json_or_yaml_file(FIXTURE / "app_translations_en.yaml")
        for channel, version in (("release", "0.25.0"), ("git", names.git_version("0123456789abcdef0123456789abcdef01234567"))):
            child = stamp.stamp(template, "garage", version, channel)
            # what the manager writes, read back the way the Supervisor reads it
            import yaml

            child_raw = yaml.safe_load(stamp.dump(child, "check"))
            label = f"instance 'garage' ({channel}, {version})"
            stamped = self.validate(f"{label} against SCHEMA_APP_CONFIG", child_raw)
            if stamped is None:
                continue
            problems = []
            if stamped["slug"] != "hri_garage":
                problems.append(f"slug {stamped['slug']}")
            if channel == "git" and "image" in stamped:
                problems.append("a git build has an image")
            if channel == "release" and stamped.get("image") != template.get("image"):
                problems.append("the release lost its image")
            if str(stamped["version"]) != version:
                problems.append(f"version {stamped['version']}")
            self.section(f"{label}: slug, image and version", problems)
            self.translations(f"{label}: HRI's translations", tr, stamped)
            self.options(f"{label}: default options (AppOptions)", stamped)
            self.backup_filter(f"{label}: backup_exclude under {CONFIG_PARENT / 'local_hri_garage'}", template, stamped, "local_hri_garage")
        self.instances_side_by_side(template)
        return 1 if self.failed else 0


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.split("\n\n", 2)[1], file=sys.stderr)
        return 2
    supervisor = pathlib.Path(argv[1]).resolve()
    root = pathlib.Path(argv[2]).resolve() if len(argv) == 3 else ROOT
    if not (supervisor / "supervisor" / "apps" / "validate.py").is_file():
        print(f"{supervisor} is not a checkout of home-assistant/supervisor with supervisor/apps/", file=sys.stderr)
        return 2
    return Check(supervisor, root).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
