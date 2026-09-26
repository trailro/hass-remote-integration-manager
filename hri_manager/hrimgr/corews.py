"""Home Assistant Core's websocket API, through the Supervisor's proxy, for one question: is this user an administrator?

The Supervisor's ingress says WHO is asking (``X-Remote-User-Id``, set from the ingress session), never whether that
user is an administrator, and any logged-in user may open an app's ingress (Core lets non-administrators create an
ingress session; ``panel_admin`` only hides the panel).  So the manager asks Core: ``config/auth/list`` lists the
users with ``is_owner``, ``is_active`` and ``group_ids``, from which ``is_admin`` follows as in Core's
``auth/models.py`` (the answer has no ``is_admin`` field).  The proxy, ``ws://supervisor/core/websocket``,
authenticates the app by its Supervisor token and needs ``homeassistant_api: true`` in config.yaml.

A hard allow-list, as for the Supervisor: only the auth message and the ``config/auth/list`` command are ever sent
(``check_message``, called by the one function that sends).  The connection is opened for that one question and
closed.  The answer is cached for ``ttl`` seconds and fetched again once for an id it does not hold.  Anything
unexpected (Core unreachable, the token refused, an answer of another shape, an unknown id) raises ``CoreError``,
which the web guard turns into 403: it fails closed."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

AUTH_TYPE = "auth"
LIST_TYPE = "config/auth/list"
ALLOWED_TYPES = frozenset({AUTH_TYPE, LIST_TYPE})
ADMIN_GROUP = "system-admin"  # homeassistant/auth/const.py GROUP_ID_ADMIN
DEFAULT_TTL = 60.0
TIMEOUT = 15.0
MAX_MESSAGE = 4 * 1024 * 1024
MAX_OTHER_MESSAGES = 20  # events or pings before the answer: more than this is not the answer coming


class CoreError(Exception):
    """Core could not answer who is an administrator: the guard refuses the request."""


class NotAllowedMessage(CoreError):
    """A message outside the allow-list: refused before it was sent."""


@dataclass(frozen=True)
class User:
    id: str
    username: str | None
    is_admin: bool


def check_message(message: Any) -> None:
    """The one gate for what goes to Core: the auth message, or the config/auth/list command, with nothing else."""
    if not isinstance(message, dict):
        raise NotAllowedMessage("refused a message that is not an object")
    kind = message.get("type")
    if kind not in ALLOWED_TYPES:
        raise NotAllowedMessage(f"refused a message of type {kind!r}: not in the manager's allow-list")
    expected = {"type", "access_token"} if kind == AUTH_TYPE else {"type", "id"}
    if set(message) != expected:
        raise NotAllowedMessage(f"refused {kind!r} with the keys {sorted(message)}")
    if kind == LIST_TYPE and (type(message["id"]) is not int or message["id"] < 1):
        raise NotAllowedMessage("refused a command without a positive integer id")
    if kind == AUTH_TYPE and (not isinstance(message["access_token"], str) or not message["access_token"]):
        raise NotAllowedMessage("refused an auth message without a token")


def parse_users(result: Any) -> dict[str, User]:
    """config/auth/list's result, checked field by field: any user of another shape fails the whole answer."""
    if not isinstance(result, list):
        raise CoreError("config/auth/list did not answer a list")
    users: dict[str, User] = {}
    for entry in result:
        if not isinstance(entry, dict):
            raise CoreError("config/auth/list answered an entry that is not an object")
        uid, owner, active, groups = entry.get("id"), entry.get("is_owner"), entry.get("is_active"), entry.get("group_ids")
        username = entry.get("username")
        if (not isinstance(uid, str) or not uid or type(owner) is not bool or type(active) is not bool
                or not isinstance(groups, list) or not all(isinstance(g, str) for g in groups)
                or not (username is None or isinstance(username, str))):
            raise CoreError("config/auth/list answered a user without id, is_owner, is_active or group_ids")
        if uid in users:
            raise CoreError("config/auth/list answered the same id twice")
        # homeassistant/auth/models.py User.is_admin
        users[uid] = User(uid, username, owner or (active and ADMIN_GROUP in groups))
    return users


class CoreUsers:
    """Home Assistant's users, as Core knows them, cached for a short time."""

    def __init__(self, url: str, token: str, *, session: aiohttp.ClientSession | None = None, ttl: float = DEFAULT_TTL,
                 timeout: float = TIMEOUT, clock: Callable[[], float] = time.monotonic):
        self._url = url
        self._token = token
        self._session = session
        self._own_session = session is None
        self._ttl = ttl
        self._timeout = timeout
        self._clock = clock
        self._users: dict[str, User] | None = None
        self._fetched = 0.0
        self._generation = 0
        self._lock = asyncio.Lock()
        self.fetches = 0

    def __repr__(self) -> str:
        return f"CoreUsers({self._url!r})"

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def _fresh(self) -> bool:
        return self._users is not None and self._clock() - self._fetched < self._ttl

    async def user(self, user_id: str) -> User:
        """The user of that id; CoreError when Core cannot say, or does not know the id even after asking again."""
        if self._fresh() and user_id in self._users:
            return self._users[user_id]
        generation = self._generation
        async with self._lock:
            # a request waiting here while another fetched uses that answer: one fetch, not one per request
            if self._generation == generation or not self._fresh():
                users = await self._fetch()
                self._users, self._fetched = users, self._clock()
                self._generation += 1
            users = self._users
        if user_id not in users:
            raise CoreError("Home Assistant does not know the user of this request")
        return users[user_id]

    async def _send(self, ws: aiohttp.ClientWebSocketResponse, message: dict) -> None:
        check_message(message)
        await ws.send_str(json.dumps(message))

    @staticmethod
    async def _receive(ws: aiohttp.ClientWebSocketResponse) -> dict:
        msg = await ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise CoreError(f"Core's websocket closed or sent {msg.type.name}")
        try:
            data = json.loads(msg.data)
        except ValueError:
            raise CoreError("Core's websocket sent something that is not JSON") from None
        if not isinstance(data, dict):
            raise CoreError("Core's websocket sent something that is not an object")
        return data

    async def _fetch(self) -> dict[str, User]:
        self.fetches += 1
        try:
            async with asyncio.timeout(self._timeout):
                async with self._get_session().ws_connect(self._url, max_msg_size=MAX_MESSAGE, autoclose=True,
                                                          autoping=True) as ws:
                    hello = await self._receive(ws)
                    if hello.get("type") != "auth_required":
                        raise CoreError("Core's websocket did not ask for authentication")
                    await self._send(ws, {"type": AUTH_TYPE, "access_token": self._token})
                    answer = await self._receive(ws)
                    if answer.get("type") != "auth_ok":
                        raise CoreError("the Supervisor's proxy to Core refused the app's token (homeassistant_api?)")
                    await self._send(ws, {"id": 1, "type": LIST_TYPE})
                    for _ in range(MAX_OTHER_MESSAGES):
                        answer = await self._receive(ws)
                        if answer.get("id") == 1 and answer.get("type") == "result":
                            break
                    else:
                        raise CoreError("Core did not answer config/auth/list")
        except TimeoutError:
            raise CoreError(f"no answer from Core's websocket in {int(self._timeout)} s") from None
        except aiohttp.ClientError as err:
            raise CoreError(f"Core's websocket is unreachable: {err.__class__.__name__}") from None
        if answer.get("success") is not True:
            error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
            raise CoreError(f"config/auth/list failed: {str(error.get('code') or 'no reason given')[:60]}")
        return parse_users(answer.get("result"))
