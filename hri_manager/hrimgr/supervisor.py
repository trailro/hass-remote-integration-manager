"""The Supervisor client, and the allow-list that is the manager's security core.

The manager role (``hassio_role: manager``) lets this app's token stop, uninstall and reconfigure ANY app, read
other apps' options (their passwords among them), restart the host and delete backups.  The manager needs a small
part of that, so every call goes through ``SupervisorClient.call``, which asks ``authorize`` first, before any
network I/O:

- the method and path must match one rule of ``RULES`` (exact paths; an app's slug only in the ``local_hri_*``
  form); anything else is refused;
- a rule that changes an app needs the ``children.Managed`` of that very slug, whose marker is read from disk again
  right before the call: an app the manager did not create is never changed, even when its slug looks like one;
- a request body may only carry the keys (and values) its rule lists: the options call can set ``boot``,
  ``watchdog`` and ``ingress_panel`` and nothing else, so an instance's own options are never written; and it must
  carry the keys its rule requires: an uninstall says whether the instance's /config folder goes
  (``remove_config``), never leaving it to the Supervisor's default.

What comes back from an app's info is cut down to ``INFO_FIELDS`` before it leaves this module: the Supervisor
includes the app's options, and an HRI instance's options hold its password."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from . import children, names
from .httpread import TooLarge, read_capped

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL = "http://supervisor"
MAX_ANSWER = 4 * 1024 * 1024
_SLUG = f"(?P<slug>{names.SLUG_PATTERN})"
# a path is checked for this shape before any rule: lowercase words separated by single slashes, nothing else (no
# dot segments, no percent escapes, no query, no double slash)
PATH_RE = re.compile(r"(?:/[a-z0-9_]+)+")


@dataclass(frozen=True)
class Rule:
    method: str
    pattern: re.Pattern
    changes_app: bool = False  # needs the Managed of the slug in the path
    body: tuple[tuple[str, tuple], ...] = ()  # (key, allowed values) a JSON body may carry
    required: tuple[str, ...] = ()  # keys of ``body`` a request must carry
    timeout: float = 30

    def body_values(self) -> dict[str, tuple]:
        return dict(self.body)


_BOOL = (True, False)


def _rule(method: str, path: str, **kw) -> Rule:
    return Rule(method, re.compile(path), **kw)


RULES: tuple[Rule, ...] = (
    _rule("GET", r"/addons"),  # the installed apps: names, versions, states (no options)
    _rule("GET", r"/addons/self/info"),  # the manager's own role and version
    _rule("GET", r"/supervisor/info"),
    _rule("GET", r"/info"),
    _rule("POST", r"/store/reload", timeout=300),
    _rule("GET", rf"/store/addons/{_SLUG}"),
    _rule("POST", rf"/store/addons/{_SLUG}/install", changes_app=True, timeout=3600),
    _rule("POST", rf"/store/addons/{_SLUG}/update", changes_app=True, body=(("backup", _BOOL),), timeout=3600),
    _rule("GET", rf"/addons/{_SLUG}/info"),
    _rule("POST", rf"/addons/{_SLUG}/options", changes_app=True,
          body=(("boot", ("auto",)), ("watchdog", _BOOL), ("ingress_panel", _BOOL))),
    _rule("POST", rf"/addons/{_SLUG}/start", changes_app=True, timeout=300),
    _rule("POST", rf"/addons/{_SLUG}/stop", changes_app=True, timeout=300),
    _rule("POST", rf"/addons/{_SLUG}/restart", changes_app=True, timeout=600),
    _rule("POST", rf"/addons/{_SLUG}/uninstall", changes_app=True, body=(("remove_config", _BOOL),),
          required=("remove_config",), timeout=600),
)

# what an app's info may show: no options, no network details of other kinds
INFO_FIELDS = ("slug", "name", "version", "version_latest", "update_available", "state", "boot", "watchdog",
               "ingress", "ingress_url", "ingress_panel", "detached", "available", "url", "repository", "build")
LIST_FIELDS = ("slug", "name", "version", "version_latest", "update_available", "state", "repository", "url",
               "detached", "available", "build")

# what the definition checks read (stamp.STORE_VIEW, stamp.INSTALLED_VIEW): privilege- and identity-bearing fields,
# never the options.  The store's "version" is the INSTALLED app's; the definition's is version_latest
STORE_DEFINITION_FIELDS = ("slug", "name", "url", "version_latest", "build", "ingress", "hassio_role", "hassio_api",
                           "homeassistant_api", "auth_api", "full_access", "docker_api", "host_network", "host_pid",
                           "apparmor")
APP_DEFINITION_FIELDS = ("slug", "name", "url", "version", "build", "ingress", "hassio_role", "hassio_api",
                         "homeassistant_api", "auth_api", "full_access", "docker_api", "host_network", "host_pid",
                         "apparmor", "host_ipc", "host_uts", "host_dbus", "privileged", "devices", "uart", "usb",
                         "gpio", "video", "audio", "kernel_modules", "devicetree", "udev", "network")


# the Supervisor's texts for an app's invalid options quote the options (". Got {...}"): an instance's password
OPTIONS_ERROR_KEYS = frozenset({"app_configuration_invalid_error", "addon_configuration_invalid_error"})


def safe_message(answer: Any, path: str) -> str | None:
    """The Supervisor's error text, or, when it may quote an app's options, a generic one with the error key and the
    app's slug only."""
    if not isinstance(answer, dict):
        return None
    message, key = answer.get("message"), answer.get("error_key")
    message = message if isinstance(message, str) else None
    key = key if isinstance(key, str) else None
    if key in OPTIONS_ERROR_KEYS or (message and ". Got " in message):
        found = re.search(r"/(local_hri_[a-z0-9_]+)", path)
        return (f"the Supervisor refused the configuration of {found.group(1) if found else 'the app'} "
                f"({key or 'invalid options'}); its message is not shown because it quotes the app's options")
    return message


class NotAllowed(Exception):
    """A call outside the allow-list: refused before anything was sent."""


class SupervisorError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def authorize(method: str, path: str, body: Any = None, managed: Any = None) -> Rule:
    """The one gate: the rule a call matches, or NotAllowed.  Pure; no I/O except re-reading a marker."""
    if not isinstance(method, str) or not isinstance(path, str) or not PATH_RE.fullmatch(path):
        raise NotAllowed(f"refused {method} {path!r}: not a plain path")
    for rule in RULES:
        if rule.method != method:
            continue
        m = rule.pattern.fullmatch(path)
        if not m:
            continue
        _check_body(rule, method, path, body)
        if rule.changes_app:
            slug = m.group("slug")
            if not isinstance(managed, children.Managed) or managed.slug != slug:
                raise NotAllowed(f"refused {method} {path}: {slug} is not an instance this manager holds the marker of")
            try:
                managed.verify()
            except (children.NotManaged, names.InvalidName) as err:
                raise NotAllowed(f"refused {method} {path}: {err}") from None
        elif managed is not None:
            raise NotAllowed(f"refused {method} {path}: a read-only call takes no instance")
        return rule
    raise NotAllowed(f"refused {method} {path}: not in the manager's allow-list")


def _check_body(rule: Rule, method: str, path: str, body: Any) -> None:
    if method == "GET":
        if body is not None:
            raise NotAllowed(f"refused {method} {path}: a GET has no body")
        return
    if body is None and not rule.required:
        return
    if not isinstance(body, dict):
        raise NotAllowed(f"refused {method} {path}: the body is not an object")
    missing = [key for key in rule.required if key not in body]
    if missing:
        raise NotAllowed(f"refused {method} {path}: {', '.join(missing)} must be sent")
    allowed = rule.body_values()
    for key, value in body.items():
        if key not in allowed:
            raise NotAllowed(f"refused {method} {path}: {key!r} may not be sent")
        if not any(value is v or (type(value) is type(v) and value == v) for v in allowed[key]):
            raise NotAllowed(f"refused {method} {path}: {key}={value!r} may not be sent")


def pick(data: Any, fields: tuple[str, ...]) -> dict:
    return {k: data[k] for k in fields if isinstance(data, dict) and k in data}


class SupervisorClient:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession | None = None):
        self._base = base_url.rstrip("/")
        self._token = token
        self._session = session
        self._own_session = session is None

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def call(self, method: str, path: str, *, body: dict | None = None, managed: children.Managed | None = None) -> Any:
        """The choke point: every request to the Supervisor is made here, after ``authorize``."""
        rule = authorize(method, path, body, managed)
        session = self._get_session()
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {self._token}"},
            "timeout": aiohttp.ClientTimeout(total=rule.timeout),
            "allow_redirects": False,
        }
        if method == "POST":
            kwargs["json"] = body or {}
        try:
            async with session.request(method, self._base + path, **kwargs) as resp:
                status = resp.status
                raw = await read_capped(resp, MAX_ANSWER)
        except TooLarge:
            raise SupervisorError(f"{method} {path}: the answer is too large") from None
        except asyncio.TimeoutError:
            raise SupervisorError(f"{method} {path}: no answer from the Supervisor in {int(rule.timeout)} s") from None
        except aiohttp.ClientError as err:
            raise SupervisorError(f"{method} {path}: {err.__class__.__name__}: {err}") from None
        try:
            answer = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            answer = {}
        if status >= 400 or not isinstance(answer, dict) or answer.get("result") != "ok":
            message = safe_message(answer, path)
            raise SupervisorError(f"{method} {path}: {message or f'HTTP {status}'}", status)
        return answer.get("data")

    # read-only
    async def list_apps(self) -> list[dict]:
        data = await self.call("GET", "/addons")
        apps = data.get("addons") if isinstance(data, dict) else None
        return [pick(a, LIST_FIELDS) for a in apps or [] if isinstance(a, dict)]

    async def self_info(self) -> dict:
        return pick(await self.call("GET", "/addons/self/info"), ("slug", "version", "hassio_api", "hassio_role", "ingress"))

    async def supervisor_info(self) -> dict:
        return pick(await self.call("GET", "/supervisor/info"), ("version", "version_latest", "channel", "arch", "healthy", "supported"))

    async def host_info(self) -> dict:
        return pick(await self.call("GET", "/info"), ("supervisor", "homeassistant", "hassos", "arch", "machine"))

    async def app_info(self, slug: str) -> dict:
        return pick(await self.call("GET", f"/addons/{slug}/info"), INFO_FIELDS)

    async def store_app(self, slug: str) -> dict | None:
        """The store's entry of a local app, or None while the store does not know it.  ``version_latest`` is the
        version of the definition the store read; the Supervisor's ``version`` here is the INSTALLED app's (null
        before the install), so it is not passed on (tests/fixtures/supervisor/store_app_*.json)."""
        try:
            data = await self.call("GET", f"/store/addons/{slug}")
        except SupervisorError as err:
            if err.status in (400, 404):
                return None
            raise
        return pick(data, ("slug", "name", "version_latest", "installed", "available", "update_available", "build", "url"))

    async def store_definition(self, slug: str) -> dict:
        """The store's parsed definition of a local app, as far as the store reports it (STORE_DEFINITION_FIELDS),
        with the definition's version as ``version``: what an install or update of it takes."""
        view = pick(await self.call("GET", f"/store/addons/{slug}"), STORE_DEFINITION_FIELDS)
        if "version_latest" in view:
            view["version"] = view.pop("version_latest")
        return view

    async def app_definition(self, slug: str) -> dict:
        """The installed app's definition, as far as its info reports it (APP_DEFINITION_FIELDS: no options)."""
        return pick(await self.call("GET", f"/addons/{slug}/info"), APP_DEFINITION_FIELDS)

    async def reload_store(self) -> None:
        await self.call("POST", "/store/reload")

    # changes an instance: every one needs its Managed
    async def install(self, managed: children.Managed) -> None:
        await self.call("POST", f"/store/addons/{managed.slug}/install", managed=managed)

    async def update(self, managed: children.Managed) -> None:
        await self.call("POST", f"/store/addons/{managed.slug}/update", body={"backup": False}, managed=managed)

    async def set_options(self, managed: children.Managed, **options) -> None:
        await self.call("POST", f"/addons/{managed.slug}/options", body=options, managed=managed)

    async def start(self, managed: children.Managed) -> None:
        await self.call("POST", f"/addons/{managed.slug}/start", managed=managed)

    async def stop(self, managed: children.Managed) -> None:
        await self.call("POST", f"/addons/{managed.slug}/stop", managed=managed)

    async def restart(self, managed: children.Managed) -> None:
        await self.call("POST", f"/addons/{managed.slug}/restart", managed=managed)

    async def uninstall(self, managed: children.Managed, remove_config: bool) -> None:
        await self.call("POST", f"/addons/{managed.slug}/uninstall", body={"remove_config": bool(remove_config)}, managed=managed)
