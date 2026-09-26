"""A fake Supervisor and a fake GitHub in one aiohttp app, with in-memory state, for the tests and the dev smoke test.

    python -m tests.fakes.stub --local-apps DIR --port 8080 [--host 127.0.0.1] [--token TOKEN]

Paths: ``/sv/...`` is the Supervisor (the manager's supervisor URL is ``http://host:port/sv``), with Core's websocket
behind its proxy at ``/sv/core/websocket`` (only ``auth`` and ``config/auth/list``), ``/gh/...`` the GitHub API,
``/cl/...`` codeload.  The Supervisor part reads the local apps folder on ``POST /store/reload`` the way the real
one does (every config.* outside dot folders; slug ``local_<slug>``), and answers only the endpoints the manager's
allow-list names; everything else is a 404 recorded in ``unexpected``.  ``/_stub/...`` is for the tests' eyes only.

Its answers have the keys, the value types and the meaning of a real Supervisor's: each is built on an answer captured
from one (tests/fixtures/supervisor/, Supervisor 2026.09.2), and tests/test_stub_shapes.py compares the two.  In
particular ``GET /store/addons/<slug>`` says ``version``: the INSTALLED version (None when not installed), and
``version_latest``: the version of the definition the store read."""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import secrets

import yaml
from aiohttp import web

from .tarballs import FIXTURES, hri_files, make_tarball, sha_of

SUPERVISOR_FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "supervisor"


def captured(name: str) -> dict:
    """A real Supervisor's answer (tests/fixtures/supervisor/<name>): {method, path, status, answer}."""
    return json.loads((SUPERVISOR_FIXTURES / name).read_text(encoding="utf-8"))


# the templates every answer is built on: all the keys a real Supervisor sends, with values of their types
STORE_APP = captured("store_app_installed.json")["answer"]["data"]
LIST_APP = next(a for a in captured("addons.json")["answer"]["data"]["addons"] if a["slug"] == "local_hri_garage")
APP_INFO = captured("app_info.json")["answer"]["data"]

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


def definition_view(data: dict) -> dict:
    """The privilege-bearing fields a Supervisor reports of a definition (store info and app info), from its
    config.yaml with the Supervisor's defaults (apps/validate.py): what the manager checks around an install."""
    return {"hassio_role": data.get("hassio_role", "default"), "hassio_api": bool(data.get("hassio_api", False)),
            "homeassistant_api": bool(data.get("homeassistant_api", False)), "auth_api": bool(data.get("auth_api", False)),
            "full_access": bool(data.get("full_access", False)), "docker_api": bool(data.get("docker_api", False)),
            "host_network": bool(data.get("host_network", False)), "host_pid": bool(data.get("host_pid", False)),
            "apparmor": "disable" if data.get("apparmor") is False else "default",
            "host_ipc": bool(data.get("host_ipc", False)), "host_uts": bool(data.get("host_uts", False)),
            "host_dbus": bool(data.get("host_dbus", False)), "privileged": list(data.get("privileged") or []),
            "devices": list(data.get("devices") or []), "uart": bool(data.get("uart", False)),
            "usb": bool(data.get("usb", False)), "gpio": bool(data.get("gpio", False)), "video": bool(data.get("video", False)),
            "audio": bool(data.get("audio", False)), "kernel_modules": bool(data.get("kernel_modules", False)),
            "devicetree": bool(data.get("devicetree", False)), "udev": bool(data.get("udev", False)),
            "ingress": bool(data.get("ingress", False)), "network": dict(data.get("ports") or {})}


STORE_DEFINITION = ("hassio_role", "hassio_api", "homeassistant_api", "auth_api", "full_access", "docker_api",
                    "host_network", "host_pid", "apparmor", "ingress")


def ok(data=None) -> web.Response:
    return web.json_response({"result": "ok", "data": data if data is not None else {}})


def err(message: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"result": "error", "message": message, **extra}, status=status)


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
        self.page_size = 100  # the release list is paged: only the newest page_size are on its first page
        self.refs = {"main": sha_of("main-1")}  # branches
        self.tags = {"test-tag": sha_of("test-tag")}  # tags that are not releases
        self.moved: dict[str, str] = {}  # a release tag force-pushed to another commit: tag -> its new commit
        self.codeload_paths: list[str] = []
        self.commits: set[str] = set()  # every commit codeload served by a ref: GitHub serves it by its sha too
        self.history: dict[str, set[str]] = {}  # full ref -> every commit it named: the ref's ancestors, as far as known
        self.users: object = [dict(u) for u in USERS]  # what config/auth/list answers (tests put other shapes here)
        self.core_down = False
        self.store_frozen = False  # a reload that notices nothing, as the Supervisor after a file dated in the future
        self.ws_messages: list[object] = []  # every message the manager sent to Core
        self.ws_connections = 0
        # what the store reports of a definition over what its config.yaml says (a definition changed between the
        # manager's last look and the Supervisor's reading of it), and what an install or update then installs
        self.hri_fixture = FIXTURES  # the app/ template HRI's archives carry (tests/fixtures/hri_v*)
        self.store_override: dict[str, dict] = {}
        self.install_override: dict[str, dict] = {}

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
            readme = path.parent / "README.md"
            found[slug] = {"slug": slug, "name": data.get("name"), "version": str(data["version"]), "url": data.get("url"),
                           "image": data.get("image"), "options": data.get("options") or {}, "folder": str(path.parent),
                           "definition": definition_view(data),
                           # the Supervisor's long_description: the README.md next to the config, if any
                           "readme": readme.read_text(encoding="utf-8") if readme.is_file() else None}
        return found

    def _detached(self, slug: str) -> bool:
        """The Supervisor's is_detached: installed, and no definition of it in the store (local_hri_foreign stands for a
        local app defined in a folder the stub does not scan)."""
        return slug.startswith("local_") and slug not in self.store and slug != "local_hri_foreign"

    def _app_view(self, slug: str) -> dict:
        """An installed app as GET /addons lists it."""
        app = self.installed[slug]
        store = self.store.get(slug)
        return {**LIST_APP, "slug": slug, "name": app["name"], "version": app["version"], "state": app["state"],
                "url": app.get("url"), "repository": app["repository"], "build": bool(app.get("build", False)),
                "detached": self._detached(slug), "available": True,
                # a detached app's latest version is its installed one (data_store falls back to the app's own data)
                "version_latest": store["version"] if store else app["version"],
                "update_available": bool(store and store["version"] != app["version"])}

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
            return err(f"App {slug} does not exist", 404)
        app = self.installed[slug]
        view = self._app_view(slug)
        detached = view["detached"]
        return ok({**APP_INFO, **app.get("definition", {}), **{k: view[k] for k in ("slug", "name", "version", "version_latest", "update_available",
                                                         "state", "url", "repository", "build", "detached", "available")},
                   "hostname": slug.replace("_", "-"), "dns": [f"{slug.replace('_', '-')}.local.hass.io"],
                   "options": app.get("options", {}), "boot": app.get("boot", "auto"), "watchdog": app.get("watchdog", False),
                   "ingress_panel": app.get("ingress_panel", False),
                   "ingress_url": app.get("ingress_url", APP_INFO["ingress_url"]),
                   "ingress_entry": app.get("ingress_url", APP_INFO["ingress_url"]).rstrip("/"),
                   # a detached app has no store source: no README, icon, logo, changelog or documentation
                   "long_description": None if detached else (self.store.get(slug) or {}).get("readme"),
                   "icon": False if detached else APP_INFO["icon"], "logo": False if detached else APP_INFO["logo"],
                   "changelog": False if detached else APP_INFO["changelog"],
                   "documentation": False if detached else APP_INFO["documentation"]})

    async def addon_action(self, request):
        slug, action = request.match_info["slug"], request.match_info["action"]
        body = request.get(BODY_KEY) or {}
        if slug not in self.installed:
            return err(f"App {slug} does not exist", 404)
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
        if not self.store_frozen:
            self.store = self._scan()
        return ok()

    async def store_app(self, request):
        slug = request.match_info["slug"]
        store = self.store.get(slug)
        if not store:
            return err(f"App {slug} does not exist in the store", 404, error_key="store_app_not_found_error",
                       extra_fields={"app": slug})
        app = self.installed.get(slug)
        # version: the installed app's (None when not installed); version_latest: the definition's
        definition = {**store["definition"], **self.store_override.get(slug, {})}
        return ok({**STORE_APP, **{k: definition[k] for k in STORE_DEFINITION}, "slug": slug, "name": store["name"],
                   "repository": "local", "url": store["url"],
                   "installed": app is not None, "version": app["version"] if app else None,
                   "version_latest": store["version"], "available": True, "detached": False, "long_description": store["readme"],
                   "update_available": bool(app and app["version"] != store["version"]), "build": store["image"] is None})

    async def store_action(self, request):
        slug, action = request.match_info["slug"], request.match_info["action"]
        store = self.store.get(slug)
        if not store:
            return err(f"App {slug} does not exist in the store", 404, error_key="store_app_not_found_error",
                       extra_fields={"app": slug})
        # the real Supervisor runs an install or update as a job of its own: a client that stops waiting (the manager
        # stopped) does not stop it
        return await asyncio.shield(asyncio.ensure_future(self._store_action(slug, action, store)))

    async def _store_action(self, slug: str, action: str, store: dict) -> web.Response:
        await asyncio.sleep(self.delay)
        if action == "install":
            if slug in self.installed:
                return err("App is already installed")
            options = dict(store["options"])
            options["password"] = "child-secret-password"  # what an instance's options may hold: never shown
            self.installed[slug] = {"slug": slug, "name": store["name"], "version": store["version"], "state": "stopped",
                                    "boot": "manual", "watchdog": False, "ingress_panel": False, "url": store["url"],
                                    "repository": "local", "build": store["image"] is None, "options": options,
                                    "ingress_url": f"/api/hassio_ingress/{secrets.token_urlsafe(16)}/",
                                    "definition": {**store["definition"], **self.install_override.get(slug, {})}}
            self.kept_data.discard(slug)
        else:
            app = self.installed.get(slug)
            if not app:
                return err("App is not installed")
            if app["version"] == store["version"]:
                return err("No update available")
            app["version"] = store["version"]
            app["name"] = store["name"]
            app["definition"] = {**store["definition"], **self.install_override.get(slug, {})}
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

    @staticmethod
    def release_json(v: str) -> dict:
        return {"tag_name": f"v{v}", "name": f"v{v}", "draft": False, "prerelease": "b" in v,
                "published_at": "2026-09-26T07:08:22Z", "html_url": f"{HRI_URL}/releases/tag/v{v}"}

    async def gh_releases(self, request):
        out = [self.release_json(v) for v in reversed(self.releases)][:self.page_size]
        out.append({"tag_name": "v9.9.9", "draft": True})
        return web.json_response(out)

    async def gh_release(self, request):
        tag = request.match_info["tag"]
        if tag == "v9.9.9":
            return web.json_response({"tag_name": "v9.9.9", "draft": True})
        if not tag.startswith("v") or tag[1:] not in self.releases:
            return web.json_response({"message": "Not Found"}, status=404)
        return web.json_response(self.release_json(tag[1:]))

    def ref_sha(self, full: str) -> str | None:
        """The commit of refs/heads/<branch> or refs/tags/<tag>, as HRI's repository has them."""
        sha = self._ref_sha(full)
        if sha:
            self.history.setdefault(full, set()).add(sha)
        return sha

    def _ref_sha(self, full: str) -> str | None:
        if full.startswith("refs/heads/"):
            return self.refs.get(full[len("refs/heads/"):])
        if full.startswith("refs/tags/v") and full[len("refs/tags/v"):] in self.releases:
            return self.moved.get(full[len("refs/tags/"):]) or sha_of("tag-" + full[len("refs/tags/v"):])
        if full.startswith("refs/tags/"):
            return self.tags.get(full[len("refs/tags/"):])
        return None

    def tarball_for(self, ref: str) -> bytes | None:
        """Full refs, and a commit served before by one (the manager asks for a commit only in a Repair, the one it
        recorded); never a short name or a pull request."""
        if ref in self.commits:
            return make_tarball(f"hass-remote-integration-{ref[:7]}", hri_files("0.25.0", self.hri_fixture), ref)
        sha = self.ref_sha(ref)
        if sha is None:
            return None
        self.commits.add(sha)
        if ref.startswith("refs/tags/v"):
            version = ref[len("refs/tags/v"):]
            # at a release tag HRI's app/config.yaml still names the previous version
            return make_tarball(f"hass-remote-integration-{version}", hri_files("0.24.0", self.hri_fixture), sha)
        return make_tarball(f"hass-remote-integration-{ref.rsplit('/', 1)[-1]}", hri_files("0.25.0", self.hri_fixture), sha)

    async def codeload(self, request):
        self.codeload_paths.append(request.match_info["ref"])
        data = self.tarball_for(request.match_info["ref"])
        if data is None:
            return web.Response(status=404, text="404: Not Found")
        return web.Response(body=data, content_type="application/x-gzip")

    async def gh_compare(self, request):
        """compare/<base>...<head>: the status of head against base, as GitHub answers it ("behind": head is an
        ancestor of base).  A commit only codeload knows (a fork's, say) is "diverged"; an unknown one, 404."""
        base, _, head = request.match_info["spec"].partition("...")
        base_sha = self.ref_sha(base)
        if base_sha is None or not head:
            return web.json_response({"message": "Not Found"}, status=404)
        if head == base_sha:
            return web.json_response({"status": "identical", "ahead_by": 0, "behind_by": 0})
        if head in self.history.get(base, set()):
            return web.json_response({"status": "behind", "ahead_by": 0, "behind_by": 1})
        if head in self.commits:
            return web.json_response({"status": "diverged", "ahead_by": 1, "behind_by": 1})
        return web.json_response({"message": "Not Found"}, status=404)

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
        app.router.add_get("/gh/repos/trailro/hass-remote-integration/releases/tags/{tag}", self.gh_release)
        app.router.add_get("/gh/repos/trailro/hass-remote-integration/git/ref/{ref:.+}", self.gh_ref)
        app.router.add_get("/gh/repos/trailro/hass-remote-integration/compare/{spec:.+}", self.gh_compare)
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
