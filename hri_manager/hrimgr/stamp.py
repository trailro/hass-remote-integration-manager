"""An instance's definition: HRI's own app template, stamped.

HRI stays the source of truth.  The manager takes ``app/config.yaml`` (with DOCS.md, CHANGELOG.md, the translations
and the icons) from the HRI release or git ref and changes only what makes the copy a separate app:

- ``slug: hri_<name>``, ``name: HRI <Name>``, ``panel_title: HRI <name>``;
- ``version``: the release's own (at a release tag HRI's app/config.yaml still names the previous version until the
  Image workflow moves it), or ``0.0.0-<sha>`` for a git build;
- ``ports``: every port unpublished (``null``); the sidebar panel works through ingress, and a port can be mapped on
  the app's Network tab;
- ``backup_exclude``: the ``*_hass_remote_integration/`` prefix becomes ``*_hri_<name>/``, the folder of this
  instance, or its Home Assistant backups would hold the installed Home Assistant (about 800 MB);
- ``webui`` dropped; ``image`` dropped for a git build (the Supervisor builds the folder instead);
- ``host_dbus: true`` added for an instance created with Bluetooth (the manager's registry records the choice): the
  host's D-Bus, through which BlueZ offers the Bluetooth adapters.  The template itself can never add it: it is not a
  key of ``TEMPLATE_KEYS``.

Everything else (options, schema, ingress, map, homeassistant, image, arch, timeout, uart, backup_pre/backup_post...)
is kept as HRI wrote
it, after a check (``vet_template``): the template may hold only the keys this manager version knows from HRI's own
template (``TEMPLATE_KEYS``), each with a value in the range vetted for it.  A key that could give an instance more
than HRI's app has (``hassio_role``, ``full_access``, ``docker_api``, ``privileged``, ``host_network``, ``devices``,
``apparmor``, a ``map`` of another folder...) is refused with the whole definition, whichever channel it comes from:
the manager writes app definitions with the Supervisor's manager role, and a new key in HRI's template needs a new
manager version that vets it.  The YAML is written with ``yaml.safe_dump``."""

from __future__ import annotations

import errno
import fnmatch
import json
import os
import re
import reprlib
import stat
from dataclasses import dataclass
from typing import Any

import yaml

from . import children, names, tarsafe

# the version of what stamp() and the builders write: raise it with any change to them.  An instance records it (marker
# and registry), and an update at the same HRI version rewrites a definition stamped by an older manager.  The
# Supervisor applies an installed app's definition only when the app's version changes (an update at the same version
# is refused, AppNoUpdateAvailableError), so the new stamping reaches the running app at its next HRI update or rebuild
STAMP_VERSION = 1
HRI_BACKUP_PREFIX = f"*_{names.HRI_SLUG}/"
STAMPED_KEYS = ("name", "version", "slug", "panel_title", "ports", "backup_exclude")
CONFIG_SUFFIXES = (".yaml", ".yml", ".json")  # what the Supervisor's store reads as config.* (FILE_SUFFIX_CONFIGURATION)
# what the Supervisor reads next to an app's config.* besides the Dockerfile: its own AppArmor profile (which replaces
# the default one), build options (base images, arguments) and a Dockerfile per architecture (used instead of the
# Dockerfile, so without the HRI_BUILD line the manager patches).  A git tree with one at its root is refused
BUILD_FILES_RE = re.compile(r"apparmor\.txt|build\.(?:yaml|yml|json)|Dockerfile\..+")
APP_FILES = ("DOCS.md", "CHANGELOG.md", "README.md", "icon.png", "logo.png")
# HRI's Dockerfile: the commit the image was built from.  The Supervisor passes only BUILD_VERSION and BUILD_ARCH
# to a build without build.yaml (deprecated), so a git build gets its commit as the argument's default instead
HRI_BUILD_ARG = re.compile(rb"^ARG HRI_BUILD=local$", re.M)
MAX_TEMPLATE = 256 * 1024


class TemplateError(Exception):
    pass


def _string(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 500


def _bool(value: Any) -> bool:
    return type(value) is bool


def _int_in(low: int, high: int):
    return lambda value: type(value) is int and low <= value <= high


def _match(pattern: str):
    regex = re.compile(pattern)
    return lambda value: isinstance(value, str) and regex.fullmatch(value) is not None


HRI_IMAGE = "ghcr.io/trailro/hass-remote-integration"
PORT_RE = r"[1-9][0-9]{0,4}/(?:tcp|udp)"
ARCHES = frozenset({"aarch64", "amd64", "armhf", "armv7", "i386"})
# the Supervisor's option types (apps/options.py RE_SCHEMA_ELEMENT) without device(...): an option of that type maps
# the host device it names into the container
SCHEMA_ELEMENT = re.compile(r"(?:bool|email|url|port|str(?:\(\d*,\d*\))?|password(?:\(\d*,\d*\))?"
                            r"|int(?:\(-?\d*,-?\d*\))?|float(?:\(-?[\d.]*,-?[\d.]*\))?|match\([^\n]{1,200}\)|list\([^\n]{1,200}\))\??",
                            re.ASCII)


def _schema(value: Any) -> bool:
    def element(v: Any) -> bool:
        return isinstance(v, str) and SCHEMA_ELEMENT.fullmatch(v) is not None
    return isinstance(value, dict) and all(
        isinstance(k, str) and (element(v) or (isinstance(v, list) and len(v) == 1 and element(v[0])))
        for k, v in value.items())


def _options(value: Any) -> bool:
    def plain(v: Any) -> bool:
        return v is None or isinstance(v, (str, bool, int, float))
    return isinstance(value, dict) and all(
        isinstance(k, str) and (plain(v) or (isinstance(v, list) and all(plain(x) for x in v))) for k, v in value.items())


def _map(value: Any) -> bool:
    """Only the instance's own folder: app_config, as HRI maps it."""
    return isinstance(value, list) and all(
        isinstance(e, dict) and e.get("type") == "app_config" and set(e) <= {"type", "read_only"}
        and _bool(e.get("read_only", False)) for e in value)


def _ports(value: Any) -> bool:
    return isinstance(value, dict) and all(
        _match(PORT_RE)(k) and (v is None or _int_in(1, 65535)(v)) for k, v in value.items())


def _backup_command(value: Any) -> bool:
    """HRI's backup_pre / backup_post: one line of at most 512 characters.  The Supervisor runs it only inside the
    app's own container, around a hot backup of a running app (apps/app.py begin_backup / end_backup -> run_inside:
    docker exec, the string split by shlex), so it can do nothing the instance's own image cannot."""
    return (isinstance(value, str) and 0 < len(value) <= 512 and value.strip() == value
            and not any(c in value for c in "\n\r\0"))


def _ports_description(value: Any) -> bool:
    return isinstance(value, dict) and all(_match(PORT_RE)(k) and _string(v) for k, v in value.items())


# the keys of HRI's app template (tests/fixtures/hri_v0.25.0, and app/config.yaml on HRI's main when this manager
# version was made) and the values vetted for each.  Anything else is refused: hassio_role, hassio_api, full_access,
# docker_api, host_*, privileged, devices, apparmor, auth_api, homeassistant_api, kernel_modules, udev, usb, gpio,
# audio, video, environment, init, stdin, tmpfs, discovery, services...
TEMPLATE_KEYS: dict[str, Any] = {
    "name": _string,
    "version": _match(r"[0-9][0-9A-Za-z.+-]{0,40}"),
    "slug": lambda v: v == names.HRI_SLUG,
    "description": _string,
    "url": lambda v: v == names.HRI_URL,
    "homeassistant": _match(r"[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,3}"),
    "arch": lambda v: isinstance(v, list) and bool(v) and all(isinstance(a, str) and a in ARCHES for a in v),
    "image": lambda v: v == HRI_IMAGE,
    "timeout": _int_in(10, 300),
    "map": _map,
    "ingress": lambda v: v is True,
    "ingress_port": _int_in(1, 65535),
    "ingress_stream": _bool,
    "panel_icon": _match(r"mdi:[a-z0-9-]{1,64}"),
    "panel_title": _string,
    "ports": _ports,
    "ports_description": _ports_description,
    "uart": _bool,  # HRI's serial sticks: /dev/ttyUSB*, /dev/ttyACM*
    "options": _options,
    "schema": _schema,
    "backup_exclude": lambda v: isinstance(v, list) and all(_string(e) for e in v),
    "backup_pre": _backup_command,  # from HRI 0.25.2 on: copied as they are
    "backup_post": _backup_command,
    "webui": _string,  # dropped by stamp()
}


def vet_template(data: dict) -> None:
    """Refuse a template with a key this manager does not know, or a value outside the range vetted for it.  The
    refusal names the key and the value's type, never the value: YAML aliases make a value of a few hundred bytes
    that str() spells out in exponential time and memory."""
    for key, value in data.items():
        check = TEMPLATE_KEYS.get(key) if isinstance(key, str) else None
        if check is None:
            shown = repr(key[:60]) if isinstance(key, str) else f"a key of type {type(key).__name__}"
            raise TemplateError(f"HRI's app definition has {shown}, which this manager version does not accept; "
                                "update the manager")
        if not check(value):
            raise TemplateError(f"HRI's app definition has {key}: a {type(value).__name__} value this manager version "
                                "does not accept; update the manager")


# What the Supervisor reports of a definition, by the names of its API, to check that the definition it installs is
# the one the manager stamped (anyone who can write the local apps folder can change a definition after the manager
# wrote it).  GET /store/addons/<slug> (Supervisor api/store.py, _generate_app_information with extended=True)
# reports STORE_VIEW of the store's parsed definition, the one an install or update takes; it does not report
# host_ipc, host_uts, host_dbus, privileged, devices, uart, usb, gpio, video, audio, kernel_modules, devicetree,
# udev, ports, map or image (only "build", whether there is an image).  GET /addons/<slug>/info (api/apps.py
# info_data) reports INSTALLED_VIEW of the installed app; it does not report map, image, backup_pre or backup_post
# either (neither does the store).  Those, and every other key, rest on the folder checks alone: children.check_tree
# (the folder's files, hashed when written, checked again before each store reload, right before the install or
# update and right after it) and decoys (no other config.* of the local apps folder declares the slug).
STORE_VIEW = ("slug", "name", "url", "version", "build", "ingress", "hassio_role", "hassio_api", "homeassistant_api",
              "auth_api", "full_access", "docker_api", "host_network", "host_pid", "apparmor")
INSTALLED_VIEW = STORE_VIEW + ("host_ipc", "host_uts", "host_dbus", "privileged", "devices", "uart", "usb", "gpio",
                               "video", "audio", "kernel_modules", "devicetree", "udev", "network")
_SHOW = reprlib.Repr(maxlevel=2, maxlist=4, maxdict=4, maxstring=60, maxother=60)


def expected_view(config: dict, slug: str) -> dict:
    """What the Supervisor must report (STORE_VIEW, INSTALLED_VIEW) for ``config``, a definition the manager stamped:
    its own values, and the Supervisor's default (apps/validate.py) for every key vet_template refuses in a template.
    ``network``: the ports' names only (the user may map a port on the Network tab); ``apparmor``: "default", the
    Supervisor's profile (an apparmor.txt in the folder would make it "profile")."""
    return {
        "slug": slug, "name": config.get("name"), "url": config.get("url"), "version": str(config.get("version")),
        "build": "image" not in config, "ingress": config.get("ingress") is True,
        "hassio_role": "default", "hassio_api": False, "homeassistant_api": False, "auth_api": False,
        "full_access": False, "docker_api": False, "host_network": False, "host_pid": False, "apparmor": "default",
        "host_ipc": False, "host_uts": False, "host_dbus": config.get("host_dbus") is True, "privileged": [],
        "devices": [], "uart": config.get("uart") is True, "usb": False, "gpio": False, "video": False,
        "audio": False, "kernel_modules": False, "devicetree": False, "udev": False,
        "network": sorted(config.get("ports") or {}),
    }


def view_differences(view: dict, expected: dict, fields: tuple[str, ...]) -> list[str]:
    """The fields of ``view`` (what the Supervisor reported) that are not ``expected``, each as "key: got, not want";
    a field the Supervisor did not report is a difference too."""
    out = []
    for key in fields:
        want = expected[key]
        if key not in view:
            out.append(f"{key} not reported")
            continue
        got = view[key]
        if key == "network" and isinstance(got, dict):
            got = sorted(got)
        if type(got) is not type(want) or got != want:
            out.append(f"{key}: {_SHOW.repr(got)}, not {_SHOW.repr(want)}")
    return out


def parse_template(raw: bytes) -> dict:
    if len(raw) > MAX_TEMPLATE:
        raise TemplateError("HRI's app/config.yaml is larger than expected")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as err:
        raise TemplateError(f"HRI's app/config.yaml does not parse: {err}") from None
    if not isinstance(data, dict):
        raise TemplateError("HRI's app/config.yaml is not a mapping")
    missing = [k for k in STAMPED_KEYS if k not in data]
    if missing:
        raise TemplateError(f"HRI's app/config.yaml lacks {', '.join(missing)}: this manager cannot stamp it")
    if data.get("slug") != names.HRI_SLUG:
        raise TemplateError(f"HRI's app/config.yaml has slug {data.get('slug')!r}, not {names.HRI_SLUG!r}")
    if not isinstance(data.get("ports"), dict):
        raise TemplateError("HRI's app/config.yaml: ports is not a mapping")
    if not isinstance(data.get("backup_exclude"), list) or not all(isinstance(e, str) for e in data["backup_exclude"]):
        raise TemplateError("HRI's app/config.yaml: backup_exclude is not a list of strings")
    vet_template(data)
    return data


def stamp(template: dict, name: str, version: str, channel: str, bluetooth: bool = False) -> dict:
    """``bluetooth``: the instance's registry says Bluetooth (host_dbus), never the template."""
    names.validate_name(name)
    if channel not in children.CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    out: dict[str, Any] = {}
    prefix = f"*_{names.folder_name(name)}/"
    for key, value in template.items():
        if key == "webui" or (key == "image" and channel == "git"):
            continue
        if key == "name":
            value = names.display_name(name)
        elif key == "slug":
            value = names.config_slug(name)
        elif key == "version":
            value = version
        elif key == "panel_title":
            value = names.panel_title(name)
        elif key == "ports":
            value = {port: None for port in value}
        elif key == "backup_exclude":
            value = [prefix + e[len(HRI_BACKUP_PREFIX):] if e.startswith(HRI_BACKUP_PREFIX) else e for e in value]
        out[key] = value
    if any(names.HRI_SLUG in e for e in out["backup_exclude"]):
        raise TemplateError("a backup_exclude entry names HRI's slug elsewhere than at its start: not stamped")
    if bluetooth:
        out["host_dbus"] = True
    return out


def dump(config: dict, source: str) -> bytes:
    # the source is a comment line: a line break in it would write keys of its own
    if not isinstance(source, str) or "\n" in source or "\r" in source:
        raise TemplateError("the definition's source is not one line")
    head = (f"# Written by HRI Manager from {source}.\n"
            "# Do not edit: the manager rewrites this folder on every update.\n")
    return (head + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)).encode("utf-8")


def _template(archive: tarsafe.Archive) -> dict:
    if "app/config.yaml" not in archive.files:
        raise TemplateError("the archive has no app/config.yaml: not hass-remote-integration, or older than its app")
    return parse_template(archive.read("app/config.yaml"))


def app_extras(archive: tarsafe.Archive) -> dict[str, bytes]:
    """DOCS.md, CHANGELOG.md, icons and translations of app/, by their path relative to app/."""
    out = {}
    for rel in archive.files:
        if not rel.startswith("app/"):
            continue
        sub = rel[len("app/"):]
        if sub in APP_FILES or (sub.startswith("translations/") and sub.count("/") == 1
                                and os.path.splitext(sub)[1] in CONFIG_SUFFIXES):
            out[sub] = archive.read(rel)
    return out


def build_release(archive: tarsafe.Archive, dest: str, name: str, version: str, source: str,
                  bluetooth: bool = False) -> dict:
    """Fill ``dest`` with a release instance's definition: the stamped config and HRI's app files."""
    config = stamp(_template(archive), name, version, "release", bluetooth)
    children.write_file(dest, "config.yaml", dump(config, source))
    for sub, data in sorted(app_extras(archive).items()):
        children.write_file(dest, sub, data)
    return config


def is_app_config(rel: str) -> bool:
    """Whether the Supervisor's store reads ``rel`` (a POSIX path below an app folder) as an app definition, by its own
    rule (store/data.py ``_find_app_configs``): the glob ``**/config.*``, a suffix of .yaml, .yml or .json, and no
    path part that starts with a dot or is ``rootfs``.  So ``docs/config.example.yaml`` is an app, and
    ``static/config.js`` or ``rootfs/config.yaml`` are not."""
    parts = rel.split("/")
    return (fnmatch.fnmatchcase(parts[-1], "config.*") and os.path.splitext(parts[-1])[1] in CONFIG_SUFFIXES
            and not any(p.startswith(".") or p == "rootfs" for p in parts))


def build_git(archive: tarsafe.Archive, dest: str, name: str, version: str, sha: str, source: str,
              config: dict | None = None, bluetooth: bool = False) -> tuple[dict, list[str]]:
    """Fill ``dest`` with a git instance: HRI's whole tree, built by the Supervisor from its root Dockerfile.
    ``config``: the stamped config to write (Repair, from the manager's copy) instead of stamping the tree's own.

    Refused when the tree's root has a file the Supervisor would use to build or confine the app (``BUILD_FILES_RE``).
    Every file the store would read as an app (``is_app_config``: app/config.yaml, and any other config.*) goes, and
    only those: a second one would be a second app, and anything else may be what the build needs (HRI has
    static/config.js, config.css and templates/config.html).  Returns the stamped config and what was done."""
    if "Dockerfile" not in archive.files:
        raise TemplateError("the archive has no Dockerfile at its root: the Supervisor could not build it")
    build_files = sorted(rel for rel in (*archive.files, *archive.links, *archive.dirs)
                         if "/" not in rel and BUILD_FILES_RE.fullmatch(rel))
    if build_files:
        raise TemplateError(f"the tree has {', '.join(tarsafe.show(r) for r in build_files)} at its root, which the "
                            "Supervisor would use to build or confine the app instead of HRI's Dockerfile: refused")
    stamped = stamp(_template(archive), name, version, "git", bluetooth)
    config = stamped if config is None else config
    extras = app_extras(archive)
    tarsafe.extract(archive, dest)
    notes = [f"skipped link {tarsafe.show(rel)}" for rel in archive.skipped]
    for rel in find_configs(dest):
        children.remove_file(dest, rel)
        notes.append(f"removed {tarsafe.show(rel)}")
    for sub, data in sorted(extras.items()):
        try:
            children.remove_file(dest, sub)
        except FileNotFoundError:
            pass
        children.write_file(dest, sub, data)
    children.write_file(dest, "config.yaml", dump(config, source))
    text = children.read_file(dest, "Dockerfile")
    patched, count = HRI_BUILD_ARG.subn(b"ARG HRI_BUILD=" + sha.encode("ascii"), text, count=1)
    if count:
        children.remove_file(dest, "Dockerfile")
        children.write_file(dest, "Dockerfile", patched)
        notes.append(f"Dockerfile: HRI_BUILD defaults to {sha[:12]}")
    else:
        notes.append("Dockerfile: no 'ARG HRI_BUILD=local' line; the build shows as 'local'")
    return config, notes


MAX_SCAN_DEPTH = 40
MAX_SCAN_ENTRIES = 500_000
MAX_APP_CONFIG = 1024 * 1024
# the Supervisor's own loader (utils/yaml.py: CSafeLoader when PyYAML has libyaml), so both read a file alike
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class ScanError(Exception):
    """The local apps folder could not be searched for decoys: nothing may be installed, updated or started."""


@dataclass(frozen=True)
class Decoy:
    """An app definition of the local apps folder that the Supervisor's store would take for (or as) an instance's."""

    path: str  # of its config file, relative to the local apps folder
    slug: str | None  # the slug it declares; None when the manager cannot read it
    problem: str


def _show_slug(slug: str) -> str:
    return repr(slug[:60])


def in_manager_space(slug: str) -> bool:
    """Whether a slug is one an instance has or could have (``hri_*``), compared as the host names the Supervisor gives
    apps (``_`` written ``-``, and without case, as DNS names are): a looser match than the store's own key."""
    return names.host_key(slug).startswith(names.host_key(names.FOLDER_PREFIX))


def decoys(root: str) -> list[Decoy]:
    """The Supervisor's store finds local apps by globbing ``**/config.*`` over the WHOLE local apps folder
    (store/data.py ``_find_app_configs``, ``is_app_config``) and keys each by the ``slug:`` inside the file, the last
    one found winning: a ``config.*`` in any folder that declares ``hri_garage`` replaces the definition in
    ``hri_garage/``, and the manager's own install or update installs it.  Its API does not say which file an app came
    from (it keeps it, ATTR_LOCATION, and never reports it), so the manager searches the folder by the same rule: every
    such file that declares a slug in the manager's space (in_manager_space) is a decoy, except an instance folder's
    own definition, ``<slug>/config.yaml`` at the root of the folder named as its slug.  So is a ``config.*`` the
    manager cannot read (not a regular file: a FIFO could serve the Supervisor a definition; larger than
    MAX_APP_CONFIG; unreadable), while one that does not parse is not (the Supervisor cannot read it either).

    Walked by folder descriptors, never through a link to a folder (as the Supervisor's glob), skipping dot folders and
    ``rootfs``; a config file that is a link is read through it, as the Supervisor reads it.  Parsed values are never
    spelled out (YAML aliases).  ScanError when the folder cannot be searched to its end."""
    out: list[Decoy] = []
    try:
        real_root = os.path.realpath(root)
        entries = children.walk(real_root, skip=lambda n: n.startswith(".") or n == "rootfs",
                                max_depth=MAX_SCAN_DEPTH, max_entries=MAX_SCAN_ENTRIES)
        for rel, name, parent, st in entries:
            if stat.S_ISDIR(st.st_mode) or not is_app_config(rel):
                continue
            shown = tarsafe.show(rel)
            try:
                fd = children.open_regular(name, parent, follow=True)
            except (FileNotFoundError, NotADirectoryError):
                continue  # a link to nothing: the Supervisor cannot read it either
            except children.UnsafePath:
                out.append(Decoy(rel, None, f"{shown} is not a regular file, so the manager cannot check which app it "
                                            "defines"))
                continue
            except OSError as err:
                if err.errno == errno.ELOOP:
                    continue
                out.append(Decoy(rel, None, f"{shown} cannot be read ({err.strerror}), so the manager cannot check which "
                                            "app it defines"))
                continue
            with os.fdopen(fd, "rb") as fh:
                raw = fh.read(MAX_APP_CONFIG + 1)
            if len(raw) > MAX_APP_CONFIG:
                out.append(Decoy(rel, None, f"{shown} is larger than the manager reads, so it cannot check which app it "
                                            "defines"))
                continue
            try:
                data = json.loads(raw) if rel.endswith(".json") else yaml.load(raw.decode("utf-8"), Loader=_LOADER)
            except (ValueError, UnicodeDecodeError, yaml.YAMLError):
                continue
            except RecursionError:
                out.append(Decoy(rel, None, f"{shown} is nested too deeply for the manager to read"))
                continue
            slug = data.get("slug") if isinstance(data, dict) else None
            if not isinstance(slug, str) or not in_manager_space(slug) or rel.split("/") == [slug, "config.yaml"]:
                continue
            out.append(Decoy(rel, slug, f"{shown} in the local apps folder declares the slug {_show_slug(slug)}, of "
                                        "the manager's instances: the Supervisor's store may take it for the instance's "
                                        "own definition and install it. Remove it (and find out who wrote it)"))
    except (OSError, children.UnsafePath) as err:
        raise ScanError(f"the local apps folder could not be searched for other definitions of the instances: {err}") from None
    return out


def find_configs(root: str) -> list[str]:
    """The files the Supervisor's store would read as apps below ``root`` (``is_app_config``)."""
    out = []
    for folder, dirnames, filenames in os.walk(root):
        rel_folder = os.path.relpath(folder, root)
        parts = [] if rel_folder == "." else rel_folder.split(os.sep)
        out += ["/".join(parts + [f]) for f in filenames if is_app_config("/".join(parts + [f]))]
    return sorted(out)
