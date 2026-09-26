"""A fake Supervisor and a fake GitHub in one aiohttp app, with in-memory state, for the tests and the dev smoke test.

    python -m tests.fakes.stub --local-apps DIR --port 8080 [--host 127.0.0.1] [--token TOKEN]

Paths: ``/sv/...`` is the Supervisor (the manager's supervisor URL is ``http://host:port/sv``), with Core's websocket
behind its proxy at ``/sv/core/websocket`` (only ``auth`` and ``config/auth/list``), ``/gh/...`` the GitHub API,
``/cl/...`` codeload.  The Supervisor part reads the local apps folder on ``POST /store/reload`` the way the real
one does (every config.* outside dot folders; slug ``local_<slug>``), and answers only the endpoints the manager's
allow-list names; everything else is a 404 recorded in ``unexpected``.  ``/_stub/...`` is for the tests' eyes only."""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import secrets

import yaml
from aiohttp import web

from .tarballs import hri_files, make_tarball, sha_of

CONFIG_SUFFIXES = (".yaml", ".yml", ".json")
BODY_KEY = web.RequestKey("body", dict)
HRI_URL = "https://github.com/trailro/hass-remote-integration"
EXTERNAL = {
    "core_mosquitto": {"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "6.5.1", "state": "started",
                       "url": "https://github.com/home-assistant/addons", "repository": "core"},
    "5c53de3b_hass_remote_integration": {"slug": "5c53de3b_hass_remote_integration", "name": "hass-remote-integration",
                                         "version": "0.25.0", "state": "started", "url": HRI_URL, "repository": "5c53de3b"},
    "local_hri_foreign": {"slug": "local_hri_foreign", "name": "Someone's own app", "version": "1.0.0", "state": "stopped",
                          "url": "https://example.com/own-app", "repository": "local"},
}
# Home Assistant's users as config/auth/list answers them: an administrator, a user, an owner outside the admin group
# (an administrator all the same) and a deactivated administrator (not one)
ALICE_ID = "a11ce00000000000000000000000a11c"
BOB_ID = "b0b00000000000000000000000000b0b"
OLGA_ID = "01ga0000000000000000000000001ga0"
DAVE_ID = "da7e00000000000000000000000da7e0"


def core_user(uid: str, username: str | None, owner: bool = False, active: bool = True, groups=("system-users",)) -> dict:
    return {"id": uid, "username": username, "name": (username or "someone").title(), "is_owner": owner,
            "is_active": active, "local_only": False, "system_generated": False, "group_ids": list(groups),
            "credentials": [{"type": "homeassistant"}] if username else []}


USERS = [core_user(ALICE_ID, "alice", groups=("system-admin",)), core_user(BOB_ID, "bob"),
         core_user(OLGA_ID, "olga", owner=True, groups=()), core_user(DAVE_ID, "dave", active=False, groups=("system-admin",))]


def ok(data=None) -> web.Response:
    return web.json_response({"result": "ok", "data": data if data is not None else {}})


def err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"result": "error", "message": message}, status=status)


class Stub:
    def __init__(self, local_apps: str, token: str = "stub-token", externals: bool = True, delay: float = 0.0):
        self.local_apps = pathlib.Path(local_apps)
        self.token = token
        self.delay = delay
        self.store: dict[str, dict] = {}
        self.installed: dict[str, dict] = {k: dict(v) for k, v in EXTERNAL.items()} if externals else {}
        self.kept_data: set[str] = set()
        self.calls: list[tuple[str, str, dict | None]] = []
        self.unexpected: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], str] = {}  # (method, supervisor path) -> error message, once
        self.releases = ["0.24.0", "0.25.0", "0.25.1", "0.26.0b1"]
        self.refs = {"main": sha_of("main-1")}  # branches
        self.tags = {"test-tag": sha_of("test-tag")}  # tags that are not releases
        self.codeload_paths: list[str] = []
        self.users: object = [dict(u) for u in USERS]  # what config/auth/list answers (tests put other shapes here)
        self.core_down = False
        self.ws_messages: list[object] = []  # every message the manager sent to Core
        self.ws_connections = 0

    # ---------------------------------------------------------------- Supervisor

    def _scan(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        if not self.local_apps.is_dir():
            return found
        for path in sorted(self.local_apps.glob("**/config.*")):
            if path.suffix not in CONFIG_SUFFIXES:
                continue
            if any(p.startswith(".") or p == "rootfs" for p in path.relative_to(self.local_apps).parts):
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix != ".json" else json.loads(path.read_text())
            if not isinstance(data, dict) or "slug" not in data or "version" not in data:
                continue
            slug = f"local_{data['slug']}"
            found[slug] = {"slug": slug, "name": data.get("name"), "version": str(data["version"]), "url": data.get("url"),
                           "image": data.get("image"), "options": data.get("options") or {}, "folder": str(path.parent)}
        return found

    def _app_view(self, slug: str) -> dict:
        app = self.installed[slug]
        store = self.store.get(slug)
        view = {k: v for k, v in app.items() if k != "options"}
        view["detached"] = slug.startswith("local_") and slug not in self.store and slug != "local_hri_foreign"
        view["version_latest"] = store["version"] if store else app["version"]
        view["update_available"] = bool(store and store["version"] != app["version"])
        return view

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        # Core's websocket authenticates in its first message, as the Supervisor's proxy does
        if request.path.startswith("/sv/") and request.path != "/sv/core/websocket":
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                return err("unauthorized", 401)
            path = request.path[len("/sv"):]
            body = None
            if request.method == "POST" and request.can_read_body:
                body = await request.json()
            request[BODY_KEY] = body or {}
            self.calls.append((request.method, path, body))
            message = self.fail.pop((request.method, path), None)
            if message:
                return err(message)
        return await handler(request)

    async def addons(self, request):
        return ok({"addons": [self._app_view(s) for s in sorted(self.installed)]})

    async def addon_info(self, request):
        slug = request.match_info["slug"]
        if slug == "self":
            return await self.self_info(request)
        if slug not in self.installed:
            return err("App is not installed")
        app = self.installed[slug]
        return ok({**self._app_view(slug), "options": app.get("options", {}), "ingress": True,
                   "ingress_url": app.get("ingress_url"), "ingress_panel": app.get("ingress_panel", False)})

    async def addon_action(self, request):
        slug, action = request.match_info["slug"], request.match_info["action"]
        body = request.get(BODY_KEY) or {}
        if slug not in self.installed:
            return err("App is not installed")
        app = self.installed[slug]
        if action == "options":
            app.update({k: v for k, v in body.items() if k in ("boot", "watchdog", "ingress_panel")})
        elif action in ("start", "restart"):
            await asyncio.sleep(self.delay)
            app["state"] = "started"
        elif action == "stop":
            app["state"] = "stopped"
        elif action == "uninstall":
            del self.installed[slug]
            if body.get("remove_config"):
                self.kept_data.discard(slug)
            else:
                self.kept_data.add(slug)
        return ok()

    async def store_reload(self, request):
        self.store = self._scan()
        return ok()

    async def store_app(self, request):
        slug = request.match_info["slug"]
        store = self.store.get(slug)
        if not store:
            return err("App does not exist", 404)
        app = self.installed.get(slug)
        return ok({"slug": slug, "name": store["name"], "version": store["version"], "installed": app is not None,
                   "available": True, "update_available": bool(app and app["version"] != store["version"]),
                   "build": store["image"] is None, "url": store["url"]})

    async def store_action(self, request):
        slug, action = request.match_info["slug"], request.match_info["action"]
        store = self.store.get(slug)
        if not store:
            return err("App does not exist", 404)
        await asyncio.sleep(self.delay)
        if action == "install":
            if slug in self.installed:
                return err("App is already installed")
            options = dict(store["options"])
            options["password"] = "child-secret-password"  # what an instance's options may hold: never shown
            self.installed[slug] = {"slug": slug, "name": store["name"], "version": store["version"], "state": "stopped",
                                    "boot": "manual", "watchdog": False, "ingress_panel": False, "url": store["url"],
                                    "repository": "local", "build": store["image"] is None, "options": options,
                                    "ingress_url": f"/api/hassio_ingress/{secrets.token_urlsafe(16)}/"}
            self.kept_data.discard(slug)
        else:
            app = self.installed.get(slug)
            if not app:
                return err("App is not installed")
            if app["version"] == store["version"]:
                return err("No update available")
            app["version"] = store["version"]
            app["name"] = store["name"]
        return ok()

    async def self_info(self, request):
        return ok({"slug": "5c53de3b_hri_manager", "version": "0.1.0", "hassio_api": True, "hassio_role": "manager",
                   "options": {"github_token": "never-shown"}})

    async def supervisor_info(self, request):
        return ok({"version": "2026.09.3", "version_latest": "2026.09.3", "channel": "stable", "arch": "amd64", "healthy": True,
                   "supported": True})

    async def info(self, request):
        return ok({"supervisor": "2026.09.3", "homeassistant": "2026.9.3", "hassos": "18.3", "arch": "amd64", "machine": "qemux86-64"})

    async def core_websocket(self, request):
        self.ws_connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.9.3"})
        msg = await ws.receive()
        auth = json.loads(msg.data) if msg.type == web.WSMsgType.TEXT else None
        self.ws_messages.append(auth)
        if self.core_down or not isinstance(auth, dict) or auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.9.3"})
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            self.ws_messages.append(data)
            if data.get("type") == "config/auth/list":
                await ws.send_json({"id": data.get("id"), "type": "result", "success": True, "result": self.users})
            else:
                await ws.send_json({"id": data.get("id"), "type": "result", "success": False,
                                    "error": {"code": "unknown_command", "message": "Unknown command."}})
        return ws

    async def unexpected_call(self, request):
        self.unexpected.append((request.method, request.path))
        return err("not implemented by the stub", 404)

    # ---------------------------------------------------------------- GitHub

    async def gh_releases(self, request):
        out = []
        for v in reversed(self.releases):
            out.append({"tag_name": f"v{v}", "name": f"v{v}", "draft": False, "prerelease": "b" in v,
                        "published_at": "2026-09-26T07:08:22Z", "html_url": f"{HRI_URL}/releases/tag/v{v}"})
        out.append({"tag_name": "v9.9.9", "draft": True})
        return web.json_response(out)

    def ref_sha(self, full: str) -> str | None:
        """The commit of refs/heads/<branch> or refs/tags/<tag>, as HRI's repository has them."""
        if full.startswith("refs/heads/"):
            return self.refs.get(full[len("refs/heads/"):])
        if full.startswith("refs/tags/v") and full[len("refs/tags/v"):] in self.releases:
            return sha_of("tag-" + full[len("refs/tags/v"):])
        if full.startswith("refs/tags/"):
            return self.tags.get(full[len("refs/tags/"):])
        return None

    def tarball_for(self, ref: str) -> bytes | None:
        """Only full refs: the manager never asks codeload for a short name, a commit or a pull request."""
        sha = self.ref_sha(ref)
        if sha is None:
            return None
        if ref.startswith("refs/tags/v"):
            version = ref[len("refs/tags/v"):]
            # at a release tag HRI's app/config.yaml still names the previous version
            return make_tarball(f"hass-remote-integration-{version}", hri_files("0.24.0"), sha)
        return make_tarball(f"hass-remote-integration-{ref.rsplit('/', 1)[-1]}", hri_files("0.25.0"), sha)

    async def codeload(self, request):
        self.codeload_paths.append(request.match_info["ref"])
        data = self.tarball_for(request.match_info["ref"])
        if data is None:
            return web.Response(status=404, text="404: Not Found")
        return web.Response(body=data, content_type="application/x-gzip")

    async def gh_ref(self, request):
        full = "refs/" + request.match_info["ref"]
        sha = self.ref_sha(full)
        if sha is None:
            return web.json_response({"message": "Not Found"}, status=404)
        return web.json_response({"ref": full, "object": {"sha": sha, "type": "commit"}})

    # ---------------------------------------------------------------- the stub's own

    async def stub_state(self, request):
        return web.json_response({"installed": {k: {kk: vv for kk, vv in v.items() if kk != "options"} for k, v in self.installed.items()},
                                  "store": sorted(self.store), "calls": self.calls, "unexpected": self.unexpected,
                                  "kept_data": sorted(self.kept_data), "refs": self.refs})

    async def stub_control(self, request):
        body = await request.json()
        if "advance" in body:
            self.refs[body["advance"]] = sha_of(f"{body['advance']}-{secrets.token_hex(4)}")
        if "fail" in body:
            self.fail[(body["fail"][0], body["fail"][1])] = body["fail"][2]
        if "release" in body:
            self.releases.append(body["release"])
        return web.json_response({"ok": True, "refs": self.refs})

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth])
        slug = r"{slug:[A-Za-z0-9_.-]+}"
        app.router.add_get("/sv/addons", self.addons)
        app.router.add_get(f"/sv/addons/{slug}/info", self.addon_info)
        app.router.add_post(f"/sv/addons/{slug}/{{action:options|start|stop|restart|uninstall}}", self.addon_action)
        app.router.add_post("/sv/store/reload", self.store_reload)
        app.router.add_get(f"/sv/store/addons/{slug}", self.store_app)
        app.router.add_post(f"/sv/store/addons/{slug}/{{action:install|update}}", self.store_action)
        app.router.add_get("/sv/supervisor/info", self.supervisor_info)
        app.router.add_get("/sv/info", self.info)
        app.router.add_get("/sv/core/websocket", self.core_websocket)
        app.router.add_get("/gh/repos/trailro/hass-remote-integration/releases", self.gh_releases)
        app.router.add_get("/gh/repos/trailro/hass-remote-integration/git/ref/{ref:.+}", self.gh_ref)
        app.router.add_get("/cl/trailro/hass-remote-integration/tar.gz/{ref:.+}", self.codeload)
        app.router.add_get("/_stub/state", self.stub_state)
        app.router.add_post("/_stub/control", self.stub_control)
        app.router.add_route("*", "/{tail:.*}", self.unexpected_call)
        return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-apps", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--token", default="stub-token")
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()
    web.run_app(Stub(args.local_apps, args.token, delay=args.delay).app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
