"""The web UI and API, served only through Home Assistant's ingress, and only to Home Assistant's administrators.

The app publishes no port, and a request is served only when its TRANSPORT peer (not a header) is the Supervisor,
which proxies ingress from the hassio network.  Being logged in to Home Assistant is not enough: any user may open an
app's ingress (``panel_admin`` only hides the panel), so every request, page and API alike, is checked here:

- the Supervisor sets ``X-Remote-User-Id`` (and ``X-Remote-User-Name``) from the ingress session, spelled exactly so
  (its const.py; checked on the wire with its own _init_header and aiohttp).  It drops a client's copy only in that
  exact spelling: a client's ``x-remote-user-id`` passes, and aiohttp's merge of case-variant keys then sends the
  CLIENT's value alone.  So in the raw headers each must appear at most once (the id exactly once), spelled exactly
  as the Supervisor spells it; any other spelling refuses the request (tests/test_access.py SupervisorWireTest pins
  that merge with a real ClientSession: if aiohttp kept the first spelling instead, this check would not be enough);
- Core says whether that id is an administrator (corews.py: ``config/auth/list`` through the Supervisor's proxy);
  when Core cannot say, the request is refused: the guard fails closed;
- ``allowed_users`` narrows the administrators further, keyed on the verified id: the id itself, or the login name
  Core reports for it (never the display name, which any administrator can change).  A list whose entries are all
  blank refuses everyone.  The user name header is shown and logged, never trusted.

A state-changing request needs ``X-Requested-With: fetch`` and a JSON body, which a form or a cross-site page cannot
send without a CORS preflight this app never answers.  Every URL the page uses is relative: ingress serves it under a
prefix the request never shows."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re

from aiohttp import web

from . import VERSION, names
from .corews import CoreError, CoreUsers
from .github import GitHubError
from .instances import InvalidRequest, Manager
from .jobs import Busy
from .settings import Settings
from .supervisor import NotAllowed, SupervisorError

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ASSETS = {"mgr.css": "text/css", "mgr.js": "application/javascript"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
USER_HEADER = "X-Remote-User-Name"
USER_ID_HEADER = "X-Remote-User-Id"  # the Supervisor's HEADER_REMOTE_USER_ID, byte for byte
USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
READ_ONLY = frozenset({"GET", "HEAD"})
MAX_BODY = 64 * 1024
SETTINGS_KEY = web.AppKey("settings", Settings)
MANAGER_KEY = web.AppKey("manager", Manager)
USER_KEY = web.RequestKey("user", str)  # who asks, for jobs and logs: Core's login name, else display name, else id


def _asset_version() -> str:
    h = hashlib.sha256()
    for name in sorted(ASSETS):
        with open(os.path.join(STATIC_DIR, name), "rb") as fh:
            h.update(name.encode() + fh.read())
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()


def transport_peer(request: web.BaseRequest) -> str | None:
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    if not isinstance(peer, tuple) or not peer:
        return None
    try:
        ip = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _headers(response: web.StreamResponse, path: str) -> None:
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if path.startswith("/api/") or path == "/":
        response.headers.setdefault("Cache-Control", "no-store")


def _refuse(status: int, text: str) -> web.Response:
    response = web.Response(status=status, text=text, content_type="text/plain")
    _headers(response, "")
    return response


def raw_header(request: web.BaseRequest, name: str) -> tuple[list[str], bool]:
    """The values of the header ``name`` as received, and whether every one of them was spelled exactly ``name``."""
    exact = name.encode("ascii")
    values, spelled = [], True
    for key, value in request.raw_headers:
        if key.lower() == exact.lower():
            values.append(value.decode("utf-8", "replace"))
            spelled = spelled and key == exact
    return values, spelled


async def _identify(request: web.Request, settings: Settings, users: CoreUsers) -> str | web.Response:
    """The request's Home Assistant administrator (as Core names them), or the 403 that refuses it."""
    where = f"{request.method} {request.path}"
    ids, ids_spelled = raw_header(request, USER_ID_HEADER)
    user_names, names_spelled = raw_header(request, USER_HEADER)
    if (len(ids) != 1 or len(user_names) > 1 or not ids_spelled or not names_spelled
            or not USER_ID_RE.fullmatch(ids[0])):
        _LOGGER.warning("refused %s: user headers not as the Supervisor sets them (%d id(s), %d name(s), spelled %s)",
                        where, len(ids), len(user_names), "as expected" if ids_spelled and names_spelled else "otherwise")
        return _refuse(403, "HRI Manager could not tell which Home Assistant user is asking. Open it from Home "
                            "Assistant's sidebar.")
    user_id, name = ids[0], (user_names[0] if user_names else "")
    try:
        user = await users.user(user_id)
    except CoreError as err:
        _LOGGER.warning("refused %s: could not check the user with Home Assistant: %s", where, err)
        return _refuse(403, "HRI Manager could not check with Home Assistant that you are an administrator, so it "
                            "refuses the request. Try again in a moment; the app's log says why.")
    shown = user.username or user.name or user_id
    if not user.is_admin:
        _LOGGER.warning("refused %s: Home Assistant user %r (%r) is not an administrator", where, shown[:64], name[:64])
        return _refuse(403, "HRI Manager is for Home Assistant administrators only.")
    if settings.allowed_users_unusable:
        _LOGGER.warning("refused %s: allowed_users has entries, but none names a user", where)
        return _refuse(403, "HRI Manager's allowed_users option has entries, but none is a user id or login name, so it "
                            "refuses everyone. Correct it on the app's Configuration tab.")
    # the id, verified with Core, and what Core says about it: never the user name header
    if settings.allowed_users and not user.matches(settings.allowed_users):
        _LOGGER.warning("refused %s: Home Assistant user %r (%r) is not in allowed_users", where, shown[:64], name[:64])
        return _refuse(403, "This Home Assistant user may not use HRI Manager (the app's allowed_users option).")
    return shown


def make_guard(settings: Settings, users: CoreUsers):
    @web.middleware
    async def guard(request: web.Request, handler):
        peer = transport_peer(request)
        if request.path == "/healthz" and request.method in READ_ONLY and peer in ("127.0.0.1", "::1"):
            return web.Response(text="ok", content_type="text/plain")
        if peer not in settings.peers:
            _LOGGER.warning("refused %s %s from %s: not the Supervisor's ingress", request.method, request.path, peer)
            return _refuse(403, "HRI Manager is served only through Home Assistant (the sidebar panel).")
        who = await _identify(request, settings, users)
        if isinstance(who, web.Response):
            return who
        request[USER_KEY] = who
        if request.method not in READ_ONLY:
            if request.method not in ("POST", "DELETE"):
                return _refuse(405, "method not allowed")
            if request.headers.get("X-Requested-With") != "fetch":
                return _refuse(403, "a state-changing request needs X-Requested-With: fetch")
            if request.content_type != "application/json":
                return _refuse(415, "a state-changing request sends JSON")
        try:
            response = await handler(request)
        except web.HTTPException as err:
            _headers(err, request.path)
            raise
        if isinstance(response, web.StreamResponse) and not response.prepared:
            _headers(response, request.path)
        return response

    return guard


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, separators=(",", ":")))


def _error(status: int, message: str) -> web.Response:
    return _json({"ok": False, "error": message}, status)


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    # the whole body (content.read(n) returns what has arrived so far); past client_max_size (MAX_BODY) aiohttp answers
    # 413 itself, so a body read here is never larger
    raw = await request.read()
    try:
        data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError):
        raise InvalidRequest("the request body is not JSON") from None
    if not isinstance(data, dict):
        raise InvalidRequest("the request body is not a JSON object")
    return data


def _handled(fn):
    async def wrapper(request: web.Request) -> web.StreamResponse:
        try:
            return await fn(request)
        except InvalidRequest as err:
            return _error(400, str(err))
        except Busy as err:
            return _error(409, str(err))
        except (SupervisorError, GitHubError) as err:
            return _error(502, str(err))
        except NotAllowed as err:
            _LOGGER.error("%s", err)
            return _error(500, str(err))
    return wrapper


def _manager(request: web.Request) -> Manager:
    return request.app[MANAGER_KEY]


def _job(job) -> web.Response:
    return _json({"ok": True, "job": job.as_dict()}, 202)


def _index_html() -> str:
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as fh:
        return fh.read().replace("__V__", ASSET_VERSION)


INDEX_HTML = _index_html()


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")


async def static(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if name not in ASSETS:
        return _refuse(404, "no such asset")
    cache = "public, max-age=31536000, immutable" if request.query.get("v") == ASSET_VERSION else "no-cache"
    return web.FileResponse(os.path.join(STATIC_DIR, name), headers={"Content-Type": ASSETS[name], "Cache-Control": cache})


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok", content_type="text/plain")


@_handled
async def api_status(request: web.Request) -> web.Response:
    return _json({"ok": True, **await _manager(request).status()})


@_handled
async def api_instances(request: web.Request) -> web.Response:
    return _json({"ok": True, **await _manager(request).instances()})


@_handled
async def api_releases(request: web.Request) -> web.Response:
    return _json({"ok": True, **await _manager(request).releases(refresh=request.query.get("refresh") == "1")})


@_handled
async def api_create(request: web.Request) -> web.Response:
    body = await _body(request)
    return _job(await _manager(request).create(body, request[USER_KEY]))


@_handled
async def api_action(request: web.Request) -> web.Response:
    name, action = request.match_info["name"], request.match_info["action"]
    body = await _body(request)
    manager = _manager(request)
    if action in ("start", "stop", "restart"):
        return _job(await manager.action(name, action, request[USER_KEY]))
    if action == "update":
        return _job(await manager.update(name, body, request[USER_KEY]))
    if action == "repair":
        return _job(await manager.repair(name, request[USER_KEY]))
    if action in ("install", "finish"):
        return _job(await manager.setup(name, action, request[USER_KEY]))
    if action == "forget":
        return _job(await manager.forget(name, body, request[USER_KEY]))
    return _error(404, "no such action")


@_handled
async def api_delete(request: web.Request) -> web.Response:
    body = await _body(request)
    return _job(await _manager(request).delete(request.match_info["name"], body, request[USER_KEY]))


@_handled
async def api_job(request: web.Request) -> web.Response:
    job = _manager(request).jobs.get(request.match_info["job_id"])
    if job is None:
        return _error(404, "no such job (the manager keeps the last 50)")
    return _json({"ok": True, "job": job.as_dict()})


@_handled
async def api_jobs(request: web.Request) -> web.Response:
    return _json({"ok": True, "jobs": [j.as_dict() | {"lines": j.lines[-3:]} for j in _manager(request).jobs.recent()[:20]]})


def create_app(settings: Settings, manager: Manager, users: CoreUsers) -> web.Application:
    app = web.Application(middlewares=[make_guard(settings, users)], client_max_size=MAX_BODY)
    app[SETTINGS_KEY] = settings
    app[MANAGER_KEY] = manager
    name = names.NAME_RE.pattern
    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/instances", api_instances)
    app.router.add_post("/api/instances", api_create)
    app.router.add_post(r"/api/instances/{name:%s}/{action:start|stop|restart|update|repair|install|finish|forget}" % name, api_action)
    app.router.add_delete(r"/api/instances/{name:%s}" % name, api_delete)
    app.router.add_get("/api/releases", api_releases)
    app.router.add_get("/api/jobs", api_jobs)
    app.router.add_get(r"/api/jobs/{job_id:[0-9a-f]{16}}", api_job)
    app.router.add_get("/healthz", healthz)
    _LOGGER.info("HRI Manager %s: %d allowed peer(s)%s", VERSION, len(settings.peers), ", DEVELOPMENT MODE" if settings.dev else "")
    return app
