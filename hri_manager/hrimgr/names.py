"""Instance names, and the slugs, folders, display names and versions derived from them.

An instance ``garage`` is the local app whose config.yaml says ``slug: hri_garage``, which the Supervisor calls
``local_hri_garage``, defined in the folder ``hri_garage`` of the local apps folder."""

from __future__ import annotations

import re

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,19}")
# names that would read as something else: the manager's own slug (hri_manager), and words of the API and the UI
RESERVED = frozenset({"manager", "self", "supervisor", "core", "local", "all", "api", "static", "jobs", "new"})
FOLDER_PREFIX = "hri_"
SLUG_PREFIX = "local_hri_"
# a Supervisor slug of an instance, as a regex fragment: the allow-list and every other check use this one
SLUG_PATTERN = r"local_hri_[a-z][a-z0-9_]{0,19}"
SLUG_RE = re.compile(SLUG_PATTERN)

HRI_REPO = "trailro/hass-remote-integration"
HRI_URL = f"https://github.com/{HRI_REPO}"
HRI_SLUG = "hass_remote_integration"
# the first HRI release that runs as an app with ingress and reads its options
MIN_VERSION = (0, 25, 0)

# HRI's release tags: v0.25.0, and pre-releases the way Home Assistant writes them (v0.26.0b1, v0.26.0rc1)
TAG_RE = re.compile(r"v(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:(a|b|rc)(\d{1,4}))?")
VERSION_RE = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:(a|b|rc)(\d{1,4}))?")
# a git ref the git channel may name: a branch, a tag or a commit
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
GIT_VERSION_RE = re.compile(r"0\.0\.0-([0-9a-f]{7,40})")


class InvalidName(ValueError):
    pass


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise InvalidName("A name is 1 to 20 characters: lowercase letters, digits and _, starting with a letter.")
    if name in RESERVED:
        raise InvalidName(f"'{name}' is reserved; choose another name.")
    return name


def folder_name(name: str) -> str:
    return FOLDER_PREFIX + name


def config_slug(name: str) -> str:
    """The ``slug:`` written into the instance's config.yaml."""
    return FOLDER_PREFIX + name


def supervisor_slug(name: str) -> str:
    """The slug the Supervisor gives the local app."""
    return SLUG_PREFIX + name


def name_from_slug(slug: object) -> str | None:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return None
    name = slug[len(SLUG_PREFIX):]
    return None if name in RESERVED else name


def display_name(name: str) -> str:
    return "HRI " + " ".join(word.capitalize() for word in name.split("_") if word)


def panel_title(name: str) -> str:
    return f"HRI {name}"


def parse_version(value: object) -> tuple | None:
    """0.25.0 -> (0, 25, 0, 1, 0); 0.26.0b1 -> (0, 26, 0, 0, 1): a pre-release sorts before its release."""
    if not isinstance(value, str):
        return None
    m = VERSION_RE.fullmatch(value)
    if not m:
        return None
    pre = {"a": 1, "b": 2, "rc": 3}.get(m.group(4) or "", 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1, pre * 10000 + int(m.group(5) or 0))


def version_from_tag(tag: object) -> str | None:
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        return None
    return tag[1:]


def supported_version(version: str) -> bool:
    """0.25.0 or newer; a pre-release of 0.25.0 is older than 0.25.0."""
    parsed = parse_version(version)
    return parsed is not None and parsed >= (*MIN_VERSION, 1, 0)


def validate_ref(ref: object) -> str:
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref) or ".." in ref or "//" in ref or ref.endswith(("/", ".lock", ".")):
        raise ValueError("A git ref is a branch, tag or commit: letters, digits and . _ / -, at most 100 characters.")
    return ref


def git_version(sha: str) -> str:
    """The version a git-channel instance gets: valid for a local build, and different for every commit, which is
    what makes the Supervisor offer the update (it compares versions for inequality)."""
    return f"0.0.0-{sha[:7]}"
