"""The web UI and API, served only through Home Assistant's ingress.

Home Assistant's login is the gate: the app publishes no port, and a request is served only when its TRANSPORT peer
(not a header) is the Supervisor, which proxies ingress from the hassio network.  ``allowed_users`` narrows it to
the Home Assistant users listed (the Supervisor sets ``X-Remote-User-Name`` from the ingress session and drops a
client's own).  A state-changing request needs ``X-Requested-With: fetch`` and a JSON body, which a form or a
cross-site page cannot send without a CORS preflight this app never answers.  Every URL the page uses is relative:
ingress serves it under a prefix the request never shows."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os

from aiohttp import web

from . import VERSION, names
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
READ_ONLY = frozenset({"GET", "HEAD"})
MAX_BODY = 64 * 1024
SETTINGS_KEY = web.AppKey("settings", Settings)
MANAGER_KEY = web.AppKey("manager", Manager)
USER_KEY = web.RequestKey("user", str)  # the Home Assistant user name of the request, "" when none


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


def make_guard(settings: Settings):
    @web.middleware
    async def guard(request: web.Request, handler):
        peer = transport_peer(request)
        if request.path == "/healthz" and request.method in READ_ONLY and peer in ("127.0.0.1", "::1"):
            return web.Response(text="ok", content_type="text/plain")
        if peer not in settings.peers:
            _LOGGER.warning("refused %s %s from %s: not the Supervisor's ingress", request.method, request.path, peer)
            return _refuse(403, "HRI Manager is served only through Home Assistant (the sidebar panel).")
        user = request.headers.get(USER_HEADER, "")
        if settings.allowed_users and user.casefold() not in settings.allowed_users:
            _LOGGER.warning("refused %s %s: Home Assistant user %r is not in allowed_users", request.method, request.path, user)
            return _refuse(403, "This Home Assistant user may not use HRI Manager (the app's allowed_users option).")
        request[USER_KEY] = user
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
    raw = await request.content.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise InvalidRequest("the request body is too large")
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
    return _job(_manager(request).create(body, request[USER_KEY]))


@_handled
async def api_action(request: web.Request) -> web.Response:
    name, action = request.match_info["name"], request.match_info["action"]
    body = await _body(request)
    manager = _manager(request)
    if action in ("start", "stop", "restart"):
        return _job(manager.action(name, action, request[USER_KEY]))
    if action == "update":
        return _job(manager.update(name, body, request[USER_KEY]))
    if action == "repair":
        return _job(manager.repair(name, request[USER_KEY]))
    return _error(404, "no such action")


@_handled
async def api_delete(request: web.Request) -> web.Response:
    body = await _body(request)
    return _job(_manager(request).delete(request.match_info["name"], body, request[USER_KEY]))


@_handled
async def api_job(request: web.Request) -> web.Response:
    job = _manager(request).jobs.get(request.match_info["job_id"])
    if job is None:
        return _error(404, "no such job (the manager keeps the last 50)")
    return _json({"ok": True, "job": job.as_dict()})


@_handled
async def api_jobs(request: web.Request) -> web.Response:
    return _json({"ok": True, "jobs": [j.as_dict() | {"lines": j.lines[-3:]} for j in _manager(request).jobs.recent()[:20]]})


def create_app(settings: Settings, manager: Manager) -> web.Application:
    app = web.Application(middlewares=[make_guard(settings)], client_max_size=MAX_BODY)
    app[SETTINGS_KEY] = settings
    app[MANAGER_KEY] = manager
    name = names.NAME_RE.pattern
    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/instances", api_instances)
    app.router.add_post("/api/instances", api_create)
    app.router.add_post(r"/api/instances/{name:%s}/{action:start|stop|restart|update|repair}" % name, api_action)
    app.router.add_delete(r"/api/instances/{name:%s}" % name, api_delete)
    app.router.add_get("/api/releases", api_releases)
    app.router.add_get("/api/jobs", api_jobs)
    app.router.add_get(r"/api/jobs/{job_id:[0-9a-f]{16}}", api_job)
    app.router.add_get("/healthz", healthz)
    _LOGGER.info("HRI Manager %s: %d allowed peer(s)%s", VERSION, len(settings.peers), ", DEVELOPMENT MODE" if settings.dev else "")
    return app
