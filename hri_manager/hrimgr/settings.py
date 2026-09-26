"""The manager's settings: the app's options (/data/options.json) and the Supervisor's environment.

Development mode exists only to run the manager outside Home Assistant against a fake Supervisor.  It is turned on
by environment variables named ``HRI_MANAGER_DEV_*``, which nothing in the app can set: the Supervisor sets an app's
environment only from the ``environment`` key of its config.yaml, which hri_manager/config.yaml does not have (a
test pins that), and no option becomes a variable.  Dev mode also refuses the real Supervisor URL, so it can never
point a relaxed peer check at a real Supervisor."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from dataclasses import dataclass, field

from . import github, supervisor

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_PEER = "172.30.32.2"  # the Supervisor on the hassio network: the only client ingress requests come from
DEV_PREFIX = "HRI_MANAGER_DEV_"
DEV_VARS = ("PEERS", "SUPERVISOR_URL", "GITHUB_API", "CODELOAD", "LOCAL_APPS", "DATA", "PORT")
INGRESS_PORT = 8099


class SettingsError(Exception):
    pass


@dataclass
class Settings:
    supervisor_token: str = field(repr=False)
    supervisor_url: str = supervisor.DEFAULT_URL
    local_apps: str = "/local_apps"
    data_dir: str = "/data"
    port: int = INGRESS_PORT
    peers: frozenset[str] = frozenset({SUPERVISOR_PEER})
    allowed_users: frozenset[str] = frozenset()
    # allowed_users has entries and none of them names anyone (all blank, or not text): nobody is served
    allowed_users_unusable: bool = False
    github_token: str = field(default="", repr=False)
    github_api: str = github.API_URL
    codeload: str = github.CODELOAD_URL
    debug: bool = False
    dev: bool = False
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @property
    def core_ws_url(self) -> str:
        """Core's websocket API behind the Supervisor's proxy (ws://supervisor/core/websocket)."""
        base = self.supervisor_url.rstrip("/")
        return ("wss" + base[5:] if base.startswith("https") else "ws" + base[4:]) + "/core/websocket"


def _peer(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise SettingsError(f"{DEV_PREFIX}PEERS: {value!r} is not an IP address") from None


def read_options(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        raise SettingsError(f"{path} cannot be read: {err}") from None
    return data if isinstance(data, dict) else {}


def from_environment(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    dev_given = {k[len(DEV_PREFIX):]: v for k, v in env.items() if k.startswith(DEV_PREFIX)}
    unknown = sorted(set(dev_given) - set(DEV_VARS))
    if unknown:
        raise SettingsError(f"unknown development variables: {', '.join(DEV_PREFIX + u for u in unknown)}")
    dev = bool(dev_given)
    if dev:
        url = dev_given.get("SUPERVISOR_URL", "").strip().rstrip("/")
        if not dev_given.get("PEERS") or not url:
            raise SettingsError(f"development mode needs both {DEV_PREFIX}PEERS and {DEV_PREFIX}SUPERVISOR_URL")
        if url == supervisor.DEFAULT_URL or url.startswith(supervisor.DEFAULT_URL + ":") or url.startswith(supervisor.DEFAULT_URL + "/"):
            raise SettingsError("development mode refuses the real Supervisor")
    token = env.get("SUPERVISOR_TOKEN") or env.get("HASSIO_TOKEN") or ""
    if not token:
        raise SettingsError("SUPERVISOR_TOKEN is not set: the manager runs as a Home Assistant app")
    data_dir = dev_given.get("DATA", "/data") if dev else "/data"
    options = read_options(os.path.join(data_dir, "options.json"))
    users = options.get("allowed_users") or []
    if not isinstance(users, list):
        users = [users]
    allowed = frozenset(u.strip().casefold() for u in users if isinstance(u, str) and u.strip())
    if len(allowed) < len(users):
        _LOGGER.warning("allowed_users: %d of its %d entries are blank or not text and are ignored%s", len(users) - len(allowed),
                        len(users), "; none is left, so nobody may use the manager until it is corrected" if not allowed else "")
    gh_token = str(options.get("github_token") or "")
    settings = Settings(
        supervisor_token=token,
        data_dir=data_dir,
        allowed_users=allowed,
        allowed_users_unusable=bool(users) and not allowed,
        github_token=gh_token,
        debug=bool(options.get("debug")),
        secrets=tuple(s for s in (token, gh_token) if s),
    )
    if dev:
        settings.dev = True
        settings.supervisor_url = dev_given["SUPERVISOR_URL"].strip().rstrip("/")
        settings.peers = frozenset(_peer(p) for p in dev_given["PEERS"].split(",") if p.strip())
        settings.local_apps = dev_given.get("LOCAL_APPS", settings.local_apps)
        settings.github_api = dev_given.get("GITHUB_API", settings.github_api)
        settings.codeload = dev_given.get("CODELOAD", settings.codeload)
        settings.port = int(dev_given.get("PORT", settings.port))
    return settings


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for s in secrets:
        text = text.replace(s, "***")
    return text


class Redact(logging.Filter):
    """Keeps the tokens out of a record's message, for any handler; never raises (a record whose message cannot be
    built is left to the formatter, which redacts its whole output)."""

    def __init__(self, secrets: tuple[str, ...]):
        super().__init__()
        self._secrets = tuple(s for s in secrets if len(s) >= 6)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
            if any(s in message for s in self._secrets):
                record.msg, record.args = _redact(message, self._secrets), None
        except Exception:  # noqa: BLE001 - a log call with bad arguments must not break the caller
            pass
        return True


class RedactingFormatter(logging.Formatter):
    """Formats a record, then removes the tokens from all of it: the message, the traceback (``exc_text``), the stack."""

    def __init__(self, secrets: tuple[str, ...], fmt: str = LOG_FORMAT):
        super().__init__(fmt)
        self._secrets = tuple(s for s in secrets if len(s) >= 6)

    def format(self, record: logging.LogRecord) -> str:
        try:
            text = super().format(record)
        except Exception:  # noqa: BLE001
            text = f"{record.levelname} {record.name}: a log line that could not be formatted ({record.msg!r:.200})"
        return _redact(text, self._secrets)
