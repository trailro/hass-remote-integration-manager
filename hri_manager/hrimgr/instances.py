"""What the manager does: list, create, update, start/stop/restart, delete and repair HRI instances.

Every action that changes something runs as a job (jobs.py).  An action on an instance first loads its
``children.Managed`` (the marker and registry check); the Supervisor client refuses any changing call without it.  A
create that fails is rolled back (uninstalled if it got that far, its folder removed, the store reloaded); an update
that fails puts the previous definition back.  Both also when the manager itself is stopped midway (the job's task
cancelled): the rollback then runs shielded from the cancellation, for at most ``ROLLBACK_BOUND`` seconds, and the
cancellation goes on.  An install the Supervisor finishes after that is listed as "install interrupted"; an instance
whose create stopped before its options and start (``setup_complete`` in the registry) gets "Finish setup".

Automatic repair: a restore without the local apps folder (a full backup leaves it out on current Supervisors) leaves
the instances installed and detached.  When the manager starts, and on a list at most every ``AUTO_REPAIR_INTERVAL``
seconds, every instance of the registry that is installed, detached and has no folder gets its definition written again
by a repair job (``AUTO_USER``), logged and shown on its row.  Nothing outside the registry is ever repaired."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import os
import secrets
import time
from typing import Any

import yaml

from . import VERSION, children, copies, names, stamp, tarsafe
from .github import GitHub, GitHubError, NotHRICommit, latest_stable
from .jobs import Busy, Job, JobFailed, Jobs, NeedsAttention, TagMoved, Tampered
from .registry import Registry, RegistryError
from .supervisor import NotAllowed, SupervisorClient, SupervisorError

_LOGGER = logging.getLogger(__name__)
HISTORY = 20
# the Supervisor stops an app 10 s after SIGTERM by default: a rollback when the manager stops gets less than that
ROLLBACK_BOUND = 8.0
AUTO_REPAIR_INTERVAL = 300.0
AUTO_REPAIR_MAX_DELAY = 86400.0  # an instance's automatic repair that keeps failing is tried at least once a day
AUTO_USER = "automatic repair"


class InvalidRequest(ValueError):
    pass


REGISTRY_FIELDS = ("name", "slug", "channel", "version", "ref_kind", "ref", "sha", "instance_id", "created_at", "updated_at",
                   "stamp_version", "created_by", "updated_by", "history")
HISTORY_KEYS = ("channel", "version", "ref_kind", "ref", "sha", "updated_at", "event", "by")


class Manager:
    def __init__(self, local_apps: str, supervisor: SupervisorClient, github: GitHub, jobs: Jobs, registry: Registry, *,
                 dev: bool = False, poll_interval: float = 2.0, store_timeout: float = 90.0,
                 auto_repair_interval: float | None = AUTO_REPAIR_INTERVAL):
        self.root = local_apps
        self.sv = supervisor
        self.gh = github
        self.jobs = jobs
        self.registry = registry
        self.dev = dev
        self.poll_interval = poll_interval
        self.store_timeout = store_timeout
        # the copies of the instances' definitions, next to the registry in /data (copies.py)
        self.copies_root = os.path.join(os.path.dirname(registry.path), copies.DIR_NAME)
        # None: no automatic repair (the tests of the manual one)
        self.auto_repair_interval = auto_repair_interval
        self._auto_checked: float | None = None
        self.auto_repairs: dict[str, dict] = {}  # name -> {"job": id, "at": iso} of its last automatic repair
        # name -> {"failures", "delay", "next" (monotonic), "error", "at"}: an instance whose automatic repair failed
        # waits AUTO_REPAIR_INTERVAL * 2**(failures-1) seconds, at most AUTO_REPAIR_MAX_DELAY, before the next
        self.auto_backoff: dict[str, dict] = {}

    # ------------------------------------------------------------------ reading

    async def status(self) -> dict:
        out: dict[str, Any] = {"version": VERSION, "dev": self.dev, "problems": []}
        try:
            me = await self.sv.self_info()
            out["role"] = me.get("hassio_role")
            out["role_ok"] = me.get("hassio_role") == "manager" and me.get("hassio_api") is True
        except (SupervisorError, NotAllowed) as err:
            out["role_ok"] = False
            out["problems"].append(f"the Supervisor API: {err}")
        try:
            out["supervisor"] = (await self.sv.supervisor_info()).get("version")
            host = await self.sv.host_info()
            out["homeassistant"] = host.get("homeassistant")
            out["arch"] = host.get("arch")
        except (SupervisorError, NotAllowed) as err:
            out["problems"].append(f"the Supervisor API: {err}")
        out["map_ok"] = os.path.isdir(self.root) and os.access(self.root, os.W_OK)
        if not out["map_ok"]:
            out["problems"].append(f"{self.root} is not a writable folder: the app needs the local_apps map")
        if out.get("role_ok") is False and "role" in out:
            out["problems"].append(f"the app's role is {out['role']!r}, not 'manager'")
        return out

    async def releases(self, refresh: bool = False) -> dict:
        releases = await self.gh.releases(refresh=refresh)
        latest = latest_stable(releases)
        return {"releases": releases, "latest": latest["version"] if latest else None}

    async def instances(self) -> dict:
        apps = await self.sv.list_apps()
        installed = {a.get("slug"): a for a in apps if isinstance(a.get("slug"), str)}
        latest = latest_stable(self.gh.cached_releases())
        folders = await asyncio.to_thread(children.scan, self.root, self.registry)
        try:
            registered = await asyncio.to_thread(self.registry.all)
        except RegistryError as err:
            _LOGGER.error("%s", err)
            registered = {}
        out, others, seen, repairable = [], [], set(), []
        for name, marker, problem in folders:
            slug = names.supervisor_slug(name)
            seen.add(slug)
            if marker is None:
                others.append({"slug": slug, "name": names.folder_name(name), "kind": "folder", "problem": problem,
                               "installed": slug in installed, "state": installed.get(slug, {}).get("state")})
                continue
            out.append(self._entry(name, marker, installed.get(slug), latest, registered.get(name)))
        for slug, app in sorted(installed.items()):
            name = names.name_from_slug(slug)
            if name and slug not in seen:
                entry = self._entry(name, None, app, latest)
                known = registered.get(name)
                # detached: the Supervisor has no definition of it anywhere (a hand-made local app with this slug is
                # not detached, and Repair would write a second definition of its slug); and the manager's registry
                # holds it: a detached app the manager did not create is not its to repair (Repair would adopt it)
                if app.get("url") == names.HRI_URL and app.get("detached") is True and known:
                    entry.update({k: known.get(k) for k in ("channel", "ref_kind", "ref", "sha")})
                    attention = known.get("needs_attention")
                    if isinstance(attention, dict):
                        rebuild = ("Rebuild writes the current commit of its branch or tag (when that is the installed "
                                   "commit, it only writes its definition)") if known.get("channel") == "git" \
                            else "Update writes a newer release"
                        entry["problem"] = (f"needs attention: {str(attention.get('reason'))[:300]}. Nothing was written. "
                                            f"{rebuild} and installs it; Delete uninstalls it; Repair tries again")
                        entry["needs_attention"] = True
                        entry["actions"] = ["update", "delete", "repair"]
                        out.append(entry)
                        continue
                    if known.get("interrupted") or not known.get("setup_complete"):
                        entry["problem"] = ("install interrupted: the manager stopped while the Supervisor was installing "
                                            "it. Repair writes its definition again; then Finish setup, or Delete")
                    else:
                        entry["problem"] = ("installed, but its definition folder is gone (restored without the local apps "
                                            "folder?): Repair writes it again")
                    entry["actions"] = ["repair"]
                    out.append(entry)
                    repairable.append(name)
                else:
                    others.append({"slug": slug, "name": app.get("name"), "kind": "local", "installed": True,
                                   "state": app.get("state"), "version": app.get("version"),
                                   "problem": ("not managed: detached, and not in the manager's registry (not created by "
                                               "this manager)" if app.get("detached") is True
                                               else "not managed: a local app the manager did not create")})
            elif slug.endswith("_" + names.HRI_SLUG):
                # local_hass_remote_integration: HRI built from a folder of the local apps folder, not the store's
                kind = "local_build" if slug == "local_" + names.HRI_SLUG else "published"
                others.append({"slug": slug, "name": app.get("name"), "kind": kind, "installed": True,
                               "state": app.get("state"), "version": app.get("version"),
                               "update_available": bool(app.get("update_available"))})
        # the manager's own records of an instance that is neither installed nor defined (uninstalled outside the
        # manager, its folder gone): shown, with Forget, never acted on by themselves
        kept = set(await asyncio.to_thread(copies.names_kept, self.copies_root))
        present = {names.name_from_slug(s) for s in installed} | {n for n, _, _ in folders}
        for name in sorted((set(registered) | kept) - present):
            if names.NAME_RE.fullmatch(name) and name not in names.RESERVED and not self.jobs.running_for(name):
                tampered = (registered.get(name) or {}).get("tampered")
                others.append({"slug": names.supervisor_slug(name), "name": name, "instance": name, "kind": "orphan",
                               "installed": False, "state": None, "actions": ["forget"],
                               "problem": (f"uninstalled by the manager: {str(tampered.get('reason'))[:300]}. "
                                           if isinstance(tampered, dict) else "")
                                          + ("neither installed nor defined: only the manager's "
                                           + " and ".join(w for w, on in (("registry entry", name in registered),
                                                                          ("copy of its definition", name in kept)) if on)
                                           + " are left. Forget drops them")})
        started = self._auto_repair(repairable)
        for entry in out:
            if entry["name"] in started:
                job = self.jobs.get(started[entry["name"]])
                entry["job"], entry["actions"] = job.summary(), []
            # needs attention: its problem says why and what to do; automatic repair has nothing more to say
            entry["auto_repair"] = None if entry.get("needs_attention") else self._auto_repair_note(entry["name"])
        infos = await asyncio.gather(*(self._info(e["slug"]) for e in out if e["installed"]))
        by_slug = {i.get("slug"): i for i in infos if i}
        for entry in out:
            info = by_slug.get(entry["slug"])
            if info:
                entry["ingress_url"] = info.get("ingress_url")
                entry["ingress_panel"] = bool(info.get("ingress_panel"))
                entry["watchdog"] = info.get("watchdog")
                entry["boot"] = info.get("boot")
        return {"instances": out, "others": others, "latest_release": latest["version"] if latest else None}

    def _auto_repair(self, names_: list[str]) -> dict[str, str]:
        """Start a repair job for each of ``names_`` (instances of the registry, installed, detached, without a
        folder), at most once every ``auto_repair_interval`` seconds: {name: job id} of those started.  A check with
        nothing to repair does not count: after a full restore the Supervisor starts the manager first and restores
        the instances after it, one by one, and each is repaired at the next check."""
        now = time.monotonic()
        if self.auto_repair_interval is None or not names_ or (
                self._auto_checked is not None and now - self._auto_checked < self.auto_repair_interval):
            return {}
        self._auto_checked = now
        started = {}
        for name in names_:
            backoff = self.auto_backoff.get(name)
            if backoff and now < backoff["next"]:
                continue
            try:
                job = self.jobs.start(name, "repair", AUTO_USER, lambda job, name=name: self._auto_repair_job(job, name))
            except Busy:
                continue
            _LOGGER.log(logging.DEBUG if backoff else logging.WARNING,
                        "instance %s is installed but detached, its definition gone (a restore without the local apps "
                        "folder?): writing it again automatically (job %s%s)", name, job.id,
                        f", attempt {backoff['failures'] + 1}" if backoff else "")
            self.auto_repairs[name] = {"job": job.id, "at": children.now_iso()}
            started[name] = job.id
        return started

    async def _auto_repair_job(self, job: Job, name: str) -> dict:
        job.log("started automatically: the instance is installed, detached and its definition folder is gone")
        try:
            result = await self._repair(job, name, AUTO_USER)
        except NeedsAttention as err:
            # left to the user from now on (the list does not repair it again): no back-off, no next try
            self.auto_backoff.pop(name, None)
            _LOGGER.warning("the automatic repair of %s needs attention: %s. Nothing was written, and it is not tried "
                            "again automatically: Update or Rebuild, Delete, or Repair on its row", name, err)
            raise
        except Exception as err:
            previous = self.auto_backoff.get(name, {})
            failures = previous.get("failures", 0) + 1
            delay = min(self.auto_repair_interval or AUTO_REPAIR_INTERVAL, AUTO_REPAIR_MAX_DELAY) * 2 ** min(failures - 1, 20)
            delay = min(delay, AUTO_REPAIR_MAX_DELAY)
            self.auto_backoff[name] = {"failures": failures, "delay": delay, "next": time.monotonic() + delay,
                                       "error": str(err) or err.__class__.__name__, "at": children.now_iso()}
            _LOGGER.log(logging.WARNING if failures == 1 else logging.DEBUG,
                        "the automatic repair of %s failed (%d time(s)): %s; next try in %d s", name, failures, err, delay)
            raise
        self.auto_backoff.pop(name, None)
        return result

    def _auto_repair_note(self, name: str) -> dict | None:
        """What the row shows of the instance's last automatic repair, while the manager remembers its job."""
        known = self.auto_repairs.get(name)
        job = self.jobs.get(known["job"]) if known else None
        backoff = self.auto_backoff.get(name)
        if job is None and backoff is None:
            return None
        note = {"at": known["at"], "job": job.id, "state": job.state, "error": job.error} if job else \
            {"at": backoff["at"], "job": None, "state": "failed", "error": backoff["error"]}
        if backoff and note["state"] != "running":
            # the last failure, kept after its job is forgotten, and when the next try is
            note.update(failures=backoff["failures"], error=backoff["error"],
                        next_try_in=max(0, round(backoff["next"] - time.monotonic())))
        return note

    def copy_missing(self) -> list[str]:
        """A copy in /data of every managed instance's definition the manager has none of (instances created by 0.1.0,
        or a copy that failed): the names copied."""
        done = []
        for name, marker, _ in children.scan(self.root, self.registry):
            if marker is None or os.path.lexists(copies.folder(self.copies_root, name)):
                continue
            try:
                copies.save(self.copies_root, name, children.child_path(self.root, name), marker)
            except (copies.CopyError, children.UnsafePath, OSError) as err:
                _LOGGER.warning("the copy of %s's definition was not saved: %s", name, err)
                continue
            done.append(name)
        return done

    async def auto_repair_check(self) -> None:
        """One check: the list, which starts the automatic repairs."""
        try:
            await self.instances()
        except (SupervisorError, NotAllowed, RegistryError) as err:
            _LOGGER.warning("the automatic repair check did not run: %s", err)

    async def auto_repair_loop(self) -> None:
        """From the manager's start, until it stops (cancelled): copies of the definitions it lacks, then a check
        every ``auto_repair_interval`` seconds, whether or not anyone opens the page.  Each instance's back-off holds
        (_auto_repair).  Without an interval, one check."""
        for name in await asyncio.to_thread(self.copy_missing):
            _LOGGER.info("instance %s: a copy of its definition is kept in /data now", name)
        while True:
            await self.auto_repair_check()
            if self.auto_repair_interval is None:
                return
            await asyncio.sleep(self.auto_repair_interval)

    async def _info(self, slug: str) -> dict | None:
        try:
            return await self.sv.app_info(slug)
        except (SupervisorError, NotAllowed) as err:
            _LOGGER.warning("info of %s: %s", slug, err)
            return None

    def _entry(self, name: str, marker: dict | None, app: dict | None, latest: dict | None, known: dict | None = None) -> dict:
        slug = names.supervisor_slug(name)
        job = self.jobs.running_for(name)
        entry: dict[str, Any] = {
            "name": name, "slug": slug, "managed": marker is not None, "installed": app is not None,
            "state": app.get("state") if app else None,
            "installed_version": app.get("version") if app else None,
            "update_available": bool(app and app.get("update_available")),
            "detached": bool(app and app.get("detached")),
            "channel": marker.get("channel") if marker else None,
            "version": marker.get("version") if marker else (app.get("version") if app else None),
            "ref": marker.get("ref") if marker else None,
            "ref_kind": marker.get("ref_kind") if marker else None,
            "sha": marker.get("sha") if marker else None,
            "newer_release": None, "problem": None, "ingress_url": None, "ingress_panel": False,
            "job": job.summary() if job else None, "actions": [], "auto_repair": None,
        }
        if marker is None:
            return entry
        if (marker.get("channel") == "release" and latest
                and (names.parse_version(latest["version"]) or ()) > (names.parse_version(marker.get("version")) or ())):
            entry["newer_release"] = latest["version"]
        actions = []
        if known and known.get("tag_moved"):
            entry["problem"] = str(known["tag_moved"])[:300]
        if known and isinstance(known.get("tampered"), dict):
            entry["problem"] = f"uninstalled by the manager: {str(known['tampered'].get('reason'))[:300]}"
        if app is None:
            entry["problem"] = "defined, but not installed: Install installs and starts it"
            actions.append("install")
        else:
            if known is not None and not known.get("setup_complete"):
                entry["problem"] = ("its setup was interrupted: Finish setup turns on start at boot, the Watchdog and "
                                    "the sidebar panel, and starts it")
                actions.append("finish")
            state = app.get("state")
            actions += ["restart", "stop"] if state == "started" else ["start"]
            actions.append("update")
        actions.append("delete")
        entry["actions"] = actions
        return entry

    # ------------------------------------------------------------------ validation (before a job starts)

    @staticmethod
    def check_name(name: object) -> str:
        try:
            return names.validate_name(name)
        except names.InvalidName as err:
            raise InvalidRequest(str(err)) from None

    @staticmethod
    def check_ref(kind: object, ref: object) -> tuple[str, str]:
        try:
            return names.validate_ref(kind, ref)
        except ValueError as err:
            raise InvalidRequest(str(err)) from None

    def check_create(self, body: dict) -> tuple[str, str, str | None, tuple[str, str] | None]:
        name = self.check_name(body.get("name"))
        channel = body.get("channel", "release")
        if channel == "release":
            version = body.get("version")
            if not isinstance(version, str) or not names.parse_version(version):
                raise InvalidRequest("Choose an HRI release.")
            if not names.supported_version(version):
                raise InvalidRequest("Instances need HRI 0.25.0 or newer (the first release that runs as an app).")
            return name, channel, version, None
        if channel == "git":
            return name, channel, None, self.check_ref(body.get("ref_kind"), body.get("ref"))
        raise InvalidRequest("The channel is 'release' or 'git'.")

    def managed(self, name: str) -> children.Managed:
        try:
            return children.load_managed(self.root, self.check_name(name), self.registry)
        except children.NotManaged as err:
            raise InvalidRequest(f"{name} is not an instance of this manager: {err}") from None

    # ------------------------------------------------------------------ jobs

    def create(self, body: dict, user: str) -> Job:
        name, channel, version, ref = self.check_create(body)
        return self.jobs.start(name, "create", user, lambda job: self._create(job, name, channel, version, ref, user))

    def action(self, name: str, action: str, user: str) -> Job:
        managed = self.managed(name)
        return self.jobs.start(name, action, user, lambda job: self._simple(job, managed, action))

    def _detached_entry(self, name: str) -> dict | None:
        """The registry's entry of ``name`` when its folder is gone (a detached instance), else None."""
        name = self.check_name(name)
        if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
            return None
        try:
            return self.registry.get(name)
        except RegistryError as err:
            raise InvalidRequest(str(err)) from None

    def update(self, name: str, body: dict, user: str) -> Job:
        detached = self._detached_entry(name)
        channel = detached.get("channel") if detached else None
        if detached is None:
            managed = self.managed(name)
            channel = managed.marker["channel"]
        version, ref = body.get("version"), None
        if channel == "release":
            if version is not None and (not isinstance(version, str) or not names.parse_version(version)):
                raise InvalidRequest("Choose an HRI release.")
        else:
            version = None
            if body.get("ref") is not None:
                ref = self.check_ref(body.get("ref_kind"), body.get("ref"))
        if detached is not None:
            # an instance Repair could not rewrite at its installed version: a newer one, written and installed at once
            return self.jobs.start(name, "update", user, lambda job: self._update_detached(job, name, version, ref, user))
        return self.jobs.start(name, "update", user, lambda job: self._update(job, managed, version, ref, user))

    def delete(self, name: str, body: dict, user: str) -> Job:
        detached = self._detached_entry(name)
        managed = self.managed(name) if detached is None else None
        remove_data = body.get("remove_data", False)
        if not isinstance(remove_data, bool):
            raise InvalidRequest("remove_data is true or false.")
        # every delete: the uninstall removes the instance's options (its password, ingress_users) with or without
        # its data folder, and a new instance of the same name would get that folder without them
        if body.get("confirm") != name:
            raise InvalidRequest(f"Deleting an instance needs its name typed: {name}.")
        if managed is None:
            return self.jobs.start(name, "delete", user, lambda job: self._delete_detached(job, name, remove_data, user))
        return self.jobs.start(name, "delete", user, lambda job: self._delete(job, managed, remove_data))

    def repair(self, name: str, user: str) -> Job:
        name = self.check_name(name)
        try:
            known = self.registry.get(name)
        except RegistryError as err:
            raise InvalidRequest(str(err)) from None
        if known is None:
            raise InvalidRequest(f"{name} is not in the manager's registry: not created by this manager, so it is not "
                                 "repaired")
        return self.jobs.start(name, "repair", user, lambda job: self._repair(job, name, user))

    def forget(self, name: str, body: dict, user: str) -> Job:
        """Drop the registry entry and the copy of an instance that is neither installed nor defined (checked again in
        the job).  Needs the name typed."""
        name = self.check_name(name)
        if body.get("confirm") != name:
            raise InvalidRequest(f"Forgetting an instance needs its name typed: {name}.")
        if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
            raise InvalidRequest(f"{names.folder_name(name)} exists: {name} is defined, not something to forget")
        return self.jobs.start(name, "forget", user, lambda job: self._forget_records(job, name))

    async def _forget_records(self, job: Job, name: str) -> dict:
        slug = names.supervisor_slug(name)
        try:
            if any(a.get("slug") == slug for a in await self.sv.list_apps()):
                raise JobFailed(f"{slug} is installed: not forgotten")
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
            raise JobFailed(f"{names.folder_name(name)} exists: not forgotten")
        try:
            known = await asyncio.to_thread(self.registry.get, name)
            if known is not None:
                await asyncio.to_thread(self.registry.remove, name)
            await asyncio.to_thread(copies.remove, self.copies_root, name)
        except (RegistryError, OSError) as err:
            raise JobFailed(str(err)) from None
        self._clear_auto(name)
        job.log(f"forgot {name}: " + ("its registry entry and " if known else "") + "any copy of its definition")
        return {"forgotten": name}

    def setup(self, name: str, action: str, user: str) -> Job:
        """``install`` (a definition without its app) or ``finish`` (installed, but its create stopped before the
        options and the start)."""
        managed = self.managed(name)
        return self.jobs.start(name, action, user, lambda job: self._setup(job, managed, install=action == "install"))

    def pending_setup(self) -> list[str]:
        """Instances whose create stopped between the install and the start (logged when the manager starts)."""
        try:
            return sorted(n for n, e in self.registry.all().items() if not e.get("setup_complete") and not e.get("interrupted")
                          and os.path.isdir(os.path.join(self.root, names.folder_name(n))))
        except RegistryError:
            return []

    # ------------------------------------------------------------------ job bodies

    async def _check_tree(self, slug: str, manifest: dict | None) -> None:
        """JobFailed when the definition folder is no longer what the manager wrote (``manifest``, None: not checked)."""
        if manifest is None:
            return
        try:
            await asyncio.to_thread(children.check_tree, self.root, names.name_from_slug(slug), manifest)
        except children.DefinitionChanged as err:
            message = (f"{err}: refused, nothing installed or updated. Anyone who can write the local apps folder (the "
                       "addons share, SSH, another app that maps it) can change a definition; find out who did")
            _LOGGER.error("%s", message)
            raise JobFailed(message) from None

    async def _wait_store(self, job: Job, slug: str, version: str, manifest: dict | None = None) -> None:
        """Reload the store until it has ``slug`` at ``version``.  ``manifest``: what the manager wrote, checked
        again right before each reload (the store reads the folder then)."""
        await self._check_tree(slug, manifest)
        job.log("reloading the Supervisor's store")
        await self.sv.reload_store()
        waited = 0.0
        reloaded_again = False
        while True:
            entry = await self.sv.store_app(slug)
            if entry and entry.get("version_latest") == version:
                job.log(f"the store has {slug} {version}")
                return
            if waited >= self.store_timeout:
                message = f"the Supervisor's store did not show {slug} {version} within {int(self.store_timeout)} s"
                future = await asyncio.to_thread(children.newest_future, self.root)
                if future:
                    when = datetime.datetime.fromtimestamp(future[1], datetime.timezone.utc).replace(microsecond=0).isoformat()
                    message += (f": {tarsafe.show(future[0])} in the local apps folder is dated {when}, in the future, and "
                                "the Supervisor notices changes there only by a newer date. Give it the current date "
                                "(touch it) and try again")
                raise JobFailed(message)
            if not reloaded_again and waited >= self.store_timeout / 2:
                await self._check_tree(slug, manifest)
                await self.sv.reload_store()
                reloaded_again = True
            await asyncio.sleep(self.poll_interval)
            waited += self.poll_interval

    async def _fetch(self, job: Job, channel: str, version: str | None, ref: tuple[str, str] | None,
                     recorded: str | None = None):
        """The source archive.  ``recorded``: the commit this instance got from the same release tag before; a tag
        that names another commit now was moved (force-pushed), and is refused."""
        expected = None
        try:
            if channel == "release":
                job.log(f"downloading hass-remote-integration v{version}")
                archive, url = await self.gh.tarball("tag", f"v{version}")
            else:
                kind, name = ref
                job.log(f"checking that hass-remote-integration has the {kind} {name}")
                expected = await self.gh.resolve_ref(kind, name)
                job.log(f"downloading hass-remote-integration at the {kind} {name}")
                archive, url = await self.gh.tarball(kind, name)
        except GitHubError as err:
            raise JobFailed(str(err)) from None
        except Exception as err:  # tarsafe.UnsafeArchive and the like
            raise JobFailed(f"the download was refused: {err}") from None
        if channel == "git" and not archive.sha:
            raise JobFailed("the archive does not say which commit it is")
        if expected and archive.sha != expected:
            raise JobFailed(f"the {ref[0]} {ref[1]} moved while it was downloaded ({expected[:12]}, then "
                            f"{archive.sha[:12]}): try again")
        if channel == "release" and recorded and archive.sha != recorded:
            raise TagMoved(f"tag moved: v{version} was commit {recorded[:12]} when this instance got it and is "
                           f"{(archive.sha or 'unknown')[:12]} now; refused (check HRI's release before trusting it)")
        return archive, url

    async def _flag_moved(self, name: str, err: TagMoved) -> None:
        try:
            await asyncio.to_thread(self.registry.update, name, tag_moved=str(err))
        except RegistryError as err2:
            _LOGGER.error("%s", err2)

    async def _check_release(self, version: str) -> None:
        try:
            release = await self.gh.release(f"v{version}")
        except GitHubError as err:
            raise JobFailed(f"the release v{version}: {err}") from None
        if release is None:
            raise JobFailed(f"hass-remote-integration {version} is not a published release (0.25.0 or newer)")

    def _marker(self, name: str, channel: str, version: str, ref: tuple[str, str], sha: str | None, source: str, user: str,
                previous: dict | None = None, instance_id: str | None = None) -> dict:
        now = children.now_iso()
        marker = {
            "manager": children.MANAGER_ID, "manager_version": VERSION, "name": name, "slug": names.supervisor_slug(name),
            "channel": channel, "version": version, "ref_kind": ref[0], "ref": ref[1], "sha": sha,
            "instance_id": (previous or {}).get("instance_id") or instance_id or secrets.token_hex(16),
            "stamp_version": stamp.STAMP_VERSION,
            "template_source": source, "created_at": now, "created_by": user, "updated_at": now, "history": [],
        }
        if previous:
            marker["created_at"] = previous.get("created_at", now)
            marker["created_by"] = previous.get("created_by", user)
            history = list(previous.get("history") or [])
            history.append({k: previous.get(k) for k in ("channel", "version", "ref_kind", "ref", "sha", "updated_at")})
            marker["history"] = history[-HISTORY:]
            marker["updated_by"] = user
        return marker

    @staticmethod
    def _registry_entry(marker: dict, **extra) -> dict:
        return {**{k: marker.get(k) for k in REGISTRY_FIELDS}, "history": Manager._history(marker.get("history")),
                "setup_complete": False, **extra}

    @staticmethod
    def _history(value: object) -> list[dict]:
        """A marker's history as the registry keeps it (the marker was read from the local apps folder): its last
        HISTORY entries, each of the known keys only, with short text values."""
        items = value if isinstance(value, list) else []
        return [{k: v for k, v in item.items() if k in HISTORY_KEYS and (v is None or (isinstance(v, str) and len(v) <= 200))}
                for item in items[-HISTORY:] if isinstance(item, dict)]

    def _clear_auto(self, name: str, keep_job: str | None = None) -> None:
        """Forget what automatic repair remembers of ``name`` (its failures, and its last job unless it is
        ``keep_job``): the instance was repaired, updated or forgotten since, and the row must not say otherwise."""
        self.auto_backoff.pop(name, None)
        if keep_job is None or self.auto_repairs.get(name, {}).get("job") != keep_job:
            self.auto_repairs.pop(name, None)

    def _forget(self, name: str, instance_id: str) -> None:
        """Drop the registry's entry of ``name``, and the copy of its definition, when it is still that instance."""
        entry = self.registry.get(name)
        if entry and entry.get("instance_id") == instance_id:
            self.registry.remove(name)
            self._clear_auto(name)
            try:
                copies.remove(self.copies_root, name)
            except OSError as err:
                _LOGGER.warning("the copy of %s's definition was not removed: %s", name, err)

    async def _save_copy(self, job: Job, managed: children.Managed) -> None:
        """Keep a copy of the definition just written in /data (copies.py).  A failure costs only the offline Repair:
        the job goes on."""
        try:
            saved = await asyncio.to_thread(copies.save, self.copies_root, managed.name,
                                            children.child_path(self.root, managed.name), managed.marker)
        except (copies.CopyError, children.UnsafePath, names.InvalidName, OSError) as err:
            job.log(f"no copy of the definition kept in the manager's /data ({err}): a Repair would download it")
            _LOGGER.warning("the copy of %s's definition was not saved: %s", managed.name, err)
            return
        job.log(f"a copy of the definition is kept in the manager's /data ({len(saved)} file(s))")

    async def _usable_copy(self, job: Job, name: str, entry: dict, version: str) -> copies.Copy | None:
        try:
            return await asyncio.to_thread(copies.load, self.copies_root, name, entry, version)
        except (copies.CopyError, names.InvalidName, OSError) as err:
            job.log(f"the manager's copy of the definition is not used: {err}")
            return None

    def _builder(self, job: Job, archive, channel: str, name: str, version: str, sha: str | None, marker: dict, source: str,
                 copy: copies.Copy | None = None, built: dict | None = None):
        """``copy``: Repair from the manager's copy, of a release (its files as they are: no archive) or of a git
        instance (its stamped config over the archive of its commit).  ``built``: gets the stamped config written, as
        "config" (what the Supervisor is then checked to report)."""
        def build(tmp: str) -> dict:
            if copy is not None and channel == "release":
                # the config dumped by the manager from the checked mapping, never the copy's bytes
                config = copy.config
                children.write_file(tmp, "config.yaml", stamp.dump(config, source))
                for rel, data in sorted(copy.files.items()):
                    if rel != "config.yaml":
                        children.write_file(tmp, rel, data)
            elif channel == "release":
                config = stamp.build_release(archive, tmp, name, version, source)
            else:
                config, notes = stamp.build_git(archive, tmp, name, version, sha, source,
                                                config=copy.config if copy is not None else None)
                for note in notes:
                    job.log(note)
            if built is not None:
                built["config"] = config
            found = stamp.find_configs(tmp)
            if found != ["config.yaml"]:
                raise JobFailed(f"the definition would hold more than one app: {found}")
            return marker
        return build

    async def _create(self, job: Job, name: str, channel: str, version: str | None, ref: tuple[str, str] | None,
                      user: str) -> dict:
        slug = names.supervisor_slug(name)
        job.log(f"checking that {slug} is free")
        apps = await self.sv.list_apps()
        if any(a.get("slug") == slug for a in apps):
            raise JobFailed(f"an app {slug} is already installed")
        # the Supervisor names an app's host after its slug, _ written as -: local_hri_a-b and local_hri_a_b collide
        clash = next((a.get("slug") for a in apps if isinstance(a.get("slug"), str) and names.host_key(a["slug"]) == names.host_key(slug)), None)
        if clash:
            raise JobFailed(f"the app {clash} is installed, and its host name is the one {slug} would get: choose another name")
        if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
            raise JobFailed(f"the local apps folder already has {names.folder_name(name)}")
        await self.sv.reload_store()
        if await self.sv.store_app(slug) is not None:
            raise JobFailed(f"the store already has an app {slug} (another local app uses the slug {names.config_slug(name)})")
        if channel == "release":
            await self._check_release(version)
        archive, source = await self._fetch(job, channel, version, ref)
        sha = archive.sha
        if channel == "git":
            version = names.git_version(sha)
            job.log(f"commit {sha[:12]}: version {version} (testing build)")
        marker = self._marker(name, channel, version, ("tag", f"v{version}") if channel == "release" else ref, sha, source, user)
        job.log(f"writing {names.folder_name(name)}")
        built: dict = {}
        try:
            await asyncio.to_thread(self.registry.put, name, self._registry_entry(marker))
            managed = await asyncio.to_thread(children.write_new, self.root, name,
                                              self._builder(job, archive, channel, name, version, sha, marker, source,
                                                            built=built),
                                              self.registry)
        except (stamp.TemplateError, children.UnsafePath, children.NotManaged, RegistryError, OSError) as err:
            await asyncio.to_thread(self._forget, name, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        await self._save_copy(job, managed)
        expected = stamp.expected_view(built["config"], slug)
        try:
            await self._wait_store(job, slug, version, managed.manifest)
            await self._verify_store(job, managed, expected, managed.manifest)
            job.log("installing (a git build takes several minutes)" if channel == "git" else "installing (pulling the image)")
            await self.sv.install(managed)
            await self._verify_installed(job, managed, expected)
            await self._finish_setup(job, managed)
        except asyncio.CancelledError:
            job.log("the manager is stopping: rolling back")
            await self._shielded(job, self._rollback_create(job, managed, interrupted=True), "the rollback")
            raise
        except Exception as err:
            job.log(f"{err}: rolling back")
            tampered = str(err) if isinstance(err, Tampered) else None
            await self._rollback_create(job, managed, tampered=tampered)
            if tampered:
                raise self._tampered(managed, tampered, "and its definition removed") from None
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        # the instance is up: a failed read of its info no longer undoes it
        info = await self._info(slug) or {}
        return {"slug": slug, "version": version, "ingress_url": info.get("ingress_url"), "state": info.get("state", "started")}

    async def _finish_setup(self, job: Job, managed: children.Managed) -> None:
        job.log("turning on start at boot, the Watchdog and the sidebar panel")
        await self.sv.set_options(managed, boot="auto", watchdog=True, ingress_panel=True)
        job.log("starting")
        await self.sv.start(managed)
        await asyncio.to_thread(self.registry.update, managed.name, setup_complete=True, tampered=None)

    @staticmethod
    async def _shielded(job: Job, coro, what: str) -> None:
        """Run ``coro`` to its end although the job's task is being cancelled, for at most ROLLBACK_BOUND seconds."""
        task = asyncio.ensure_future(coro)
        try:
            await asyncio.wait_for(asyncio.shield(task), ROLLBACK_BOUND)
        except TimeoutError:
            job.log(f"{what} did not finish within {int(ROLLBACK_BOUND)} s")
        except asyncio.CancelledError:
            job.log(f"{what} was interrupted")

    async def _verify_store(self, job: Job, managed: children.Managed, expected: dict, manifest: dict | None) -> None:
        """Right before an install or update: the store's parsed definition, the one the Supervisor installs, reports
        what the manager stamped (``expected``: stamp.expected_view), and the folder is still what it wrote.
        JobFailed otherwise: nothing is installed or updated."""
        problems = stamp.view_differences(await self.sv.store_definition(managed.slug), expected, stamp.STORE_VIEW)
        if problems:
            message = (f"the Supervisor's store holds another definition of {managed.slug} than the one the manager "
                       f"wrote ({'; '.join(problems)}): refused, nothing installed or updated. Someone changed "
                       f"{names.folder_name(managed.name)}/ in the local apps folder after the manager wrote it (the "
                       "addons share, SSH, another app that maps it); find out who did")
            _LOGGER.error("%s", message)
            raise JobFailed(message)
        await self._check_tree(managed.slug, manifest)
        job.log("the store's definition is the one the manager wrote")

    async def _verify_installed(self, job: Job, managed: children.Managed, expected: dict) -> None:
        """Right after an install or update: the installed app reports what the manager stamped.  Tampered
        otherwise; the caller uninstalls it at once."""
        problems = stamp.view_differences(await self.sv.app_definition(managed.slug), expected, stamp.INSTALLED_VIEW)
        if problems:
            raise Tampered(f"the Supervisor installed another definition of {managed.slug} than the one the manager "
                           f"wrote ({'; '.join(problems)})")
        job.log("the installed definition is the one the manager wrote")

    async def _uninstall_now(self, job: Job, managed: children.Managed) -> None:
        """Uninstall an app installed from a changed definition, keeping its /config folder."""
        job.log("uninstalling it at once (its /config folder is kept)")
        try:
            await self.sv.uninstall(managed, remove_config=False)
        except Exception as err:  # noqa: BLE001 - reported; the job fails anyway
            job.log(f"the uninstall failed: {err}")
            _LOGGER.error("%s was installed from a changed definition and could not be uninstalled: %s. Uninstall it in "
                          "Settings > Apps", managed.slug, err)

    async def _mark_tampered(self, name: str, reason: str) -> None:
        try:
            await asyncio.to_thread(self.registry.update, name, tampered={"reason": reason[:500], "at": children.now_iso()})
        except RegistryError as err:
            _LOGGER.error("%s", err)

    @staticmethod
    def _tampered(managed: children.Managed, reason: str, what: str) -> Tampered:
        message = (f"{reason}. It was uninstalled at once (its /config folder is kept) {what}. Someone changed "
                   f"{names.folder_name(managed.name)}/ in the local apps folder while the manager installed it (the "
                   "addons share, SSH, another app that maps it): find out who before you install it again")
        _LOGGER.error("%s", message)
        return Tampered(message)

    async def _rollback_create(self, job: Job, managed: children.Managed, interrupted: bool = False,
                               tampered: str | None = None) -> None:
        """``tampered``: the install took a changed definition; the registry keeps the instance, marked, for its row."""
        try:
            installed = any(a.get("slug") == managed.slug for a in await self.sv.list_apps())
            if installed:
                # remove_config False: a folder of an earlier instance of the same name (deleted with its data kept)
                # was reused by this install, and a failed create must not take it
                job.log("uninstalling")
                await self.sv.uninstall(managed, remove_config=False)
            job.log("removing the definition")
            await asyncio.to_thread(children.remove, managed)
            if tampered:
                await asyncio.to_thread(self.registry.update, managed.name, setup_complete=False,
                                        tampered={"reason": tampered[:500], "at": children.now_iso()})
            elif interrupted and not installed:
                # the Supervisor may still be installing what it was asked to: if it finishes, the list shows a
                # detached app, and the registry says why
                await asyncio.to_thread(self.registry.update, managed.name, interrupted=True, setup_complete=False)
            else:
                await asyncio.to_thread(self._forget, managed.name, managed.marker["instance_id"])
            await self.sv.reload_store()
        except Exception as err:  # noqa: BLE001 - the original failure is what the job reports
            job.log(f"rollback incomplete: {err}")
            _LOGGER.error("rollback of %s incomplete: %s", managed.slug, err)

    async def _installed(self, managed: children.Managed) -> dict:
        info = await self.sv.app_info(managed.slug)
        if not info.get("version"):
            raise JobFailed(f"{managed.slug} is not installed")
        return info

    async def _simple(self, job: Job, managed: children.Managed, action: str) -> dict:
        try:
            await self._installed(managed)
            job.log(f"{action} {managed.slug}")
            await {"start": self.sv.start, "stop": self.sv.stop, "restart": self.sv.restart}[action](managed)
            info = await self.sv.app_info(managed.slug)
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        return {"state": info.get("state")}

    async def _update(self, job: Job, managed: children.Managed, version: str | None, ref: tuple[str, str] | None,
                      user: str) -> dict:
        marker = managed.marker
        channel = marker["channel"]
        restamp = False
        try:
            info = await self._installed(managed)
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        if channel == "release":
            if version is None:
                try:
                    latest = latest_stable(await self.gh.releases())
                except GitHubError as err:
                    raise JobFailed(f"the release list: {err}") from None
                if not latest:
                    raise JobFailed("no stable HRI release found")
                version = latest["version"]
            await self._check_release(version)
            # the newer of the definition and the installed app: an update the manager stopped waiting for can leave
            # the app newer than its definition
            current = max((marker.get("version"), info.get("version")), key=lambda v: names.parse_version(v) or ())
            if (names.parse_version(version) or ()) < (names.parse_version(current) or ()):
                raise JobFailed(f"{version} is older than {current}: the manager does not downgrade")
            if version == marker.get("version") and info.get("version") == version:
                if marker.get("stamp_version") == stamp.STAMP_VERSION:
                    job.log(f"already at {version}")
                    return {"version": version, "unchanged": True}
                restamp = True
            new_ref = ("tag", f"v{version}")
        else:
            new_ref = ref or self.check_ref(marker.get("ref_kind"), marker.get("ref"))
        try:
            archive, source = await self._fetch(job, channel, version, new_ref,
                                                recorded=marker.get("sha") if version == marker.get("version") else None)
        except TagMoved as err:
            await self._flag_moved(managed.name, err)
            raise
        sha = archive.sha
        if channel == "git":
            if sha == marker.get("sha") and info.get("version") == marker.get("version"):
                if marker.get("stamp_version") == stamp.STAMP_VERSION:
                    job.log(f"the {new_ref[0]} {new_ref[1]} is still {sha[:12]}: nothing to rebuild")
                    return {"version": marker.get("version"), "unchanged": True}
                restamp = True
            version = names.git_version(sha)
            if version == marker.get("version") and sha != marker.get("sha"):
                raise JobFailed(f"commit {sha[:12]} has the same version {version} as the installed {marker.get('sha', '')[:12]}: "
                                "the Supervisor would not install it")
            job.log(f"commit {sha[:12]}: version {version}")
        new_marker = self._marker(managed.name, channel, version, new_ref, sha, source, user, previous=marker)
        recorded = {"tag_moved": None, "tampered": None, "history": self._history(new_marker["history"]),
                    **{k: new_marker[k] for k in ("version", "ref_kind", "ref", "sha", "updated_at", "stamp_version",
                                                  "created_by", "updated_by")}}
        # before the swap: a manager killed before the update is recorded finds the flag at its next start, and puts the
        # previous definition back (children.cleanup_stale)
        try:
            await asyncio.to_thread(self.registry.update, managed.name,
                                    updating={"at": children.now_iso(), "fields": recorded})
        except RegistryError as err:
            raise JobFailed(f"nothing was written: {err}") from None
        job.log(f"rewriting {names.folder_name(managed.name)} (the previous definition is kept until the update succeeds)")
        built: dict = {}
        try:
            replacement = await asyncio.to_thread(
                children.replace, managed, self._builder(job, archive, channel, managed.name, version, sha, new_marker, source,
                                                         built=built))
        except (stamp.TemplateError, children.UnsafePath, children.NotManaged, OSError) as err:
            await self._clear_updating(managed.name)
            raise JobFailed(f"the definition was not written: {err}") from None
        expected = stamp.expected_view(built["config"], managed.slug)
        try:
            managed = children.load_managed(self.root, managed.name, self.registry)
            await self._wait_store(job, managed.slug, version, replacement.manifest)
            if info.get("version") != version:
                await self._verify_store(job, managed, expected, replacement.manifest)
                job.log(f"updating {info.get('version')} -> {version}" + (" (building)" if channel == "git" else ""))
                await self.sv.update(managed)
                await self._verify_installed(job, managed, expected)
            after = await self.sv.app_info(managed.slug)
            if after.get("version") != version:
                raise JobFailed(f"the Supervisor reports {after.get('version')} after the update, not {version}")
        except asyncio.CancelledError:
            job.log("the manager is stopping: putting the previous definition back")
            await self._shielded(job, self._rollback_update(job, replacement), "putting it back")
            raise
        except Tampered as err:
            job.log(f"{err}: uninstalling it, and putting the previous definition back")
            await self._uninstall_now(job, managed)
            await self._rollback_update(job, replacement)
            await self._mark_tampered(managed.name, str(err))
            raise self._tampered(managed, str(err), "and its previous definition put back: Install installs that one "
                                                    "again") from None
        except Exception as err:
            job.log(f"{err}: putting the previous definition back")
            await self._rollback_update(job, replacement)
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        warning = None
        try:
            await asyncio.to_thread(replacement.commit)
            await asyncio.to_thread(self.registry.update, managed.name, updating=None, **recorded)
        except (OSError, RegistryError) as err:
            # the app is updated and its definition in place: only the manager's own records lag, and the flag left
            # in the registry lets the next start record the update (children.cleanup_stale)
            warning = (f"updated to {version}, but the manager's records were not updated ({err}); they catch up when "
                       "the manager starts again")
            job.log(f"warning: {warning}")
            _LOGGER.warning("%s: %s", managed.name, warning)
        await self._save_copy(job, managed)
        if restamp:
            job.log(f"the definition is stamped by this manager now (stamping {marker.get('stamp_version')} -> "
                    f"{stamp.STAMP_VERSION}); the Supervisor applies it to the running app at its next version change "
                    "(an HRI update, or a rebuild of a new commit), not at the same version")
        result = {"version": version, "state": after.get("state"), "restamped": restamp}
        return {**result, "warning": warning} if warning else result

    async def _clear_updating(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.registry.update, name, updating=None)
        except RegistryError as err:
            _LOGGER.error("%s", err)

    async def _rollback_update(self, job: Job, replacement: children.Replacement) -> None:
        try:
            await asyncio.to_thread(replacement.rollback)
            await self._clear_updating(replacement.name)
            await self.sv.reload_store()
        except Exception as err:  # noqa: BLE001
            job.log(f"could not put it back: {err}")

    def _definition_on_disk(self, managed: children.Managed) -> tuple[dict, dict]:
        """For Install of a definition this job did not write: what its folder holds now (children.digest_tree), and
        what the Supervisor must report for its config.yaml, which must be a definition this manager writes
        (copies.check).  JobFailed otherwise."""
        folder = children.child_path(self.root, managed.name)
        manifest = children.digest_tree(folder)
        try:
            raw = copies.read_file(os.path.join(folder, "config.yaml"))
            if manifest.get("config.yaml") != "sha256:" + hashlib.sha256(raw).hexdigest():
                raise copies.CopyError("its config.yaml changed while it was read")
            config = copies.check(yaml.safe_load(raw.decode("utf-8")), managed.name, str(managed.marker.get("version")),
                                  managed.entry.get("channel"))
        except (copies.CopyError, OSError, UnicodeDecodeError, yaml.YAMLError) as err:
            raise JobFailed(f"{names.folder_name(managed.name)} is not a definition this manager writes ({err}): not "
                            "installed; Delete removes it") from None
        return stamp.expected_view(config, managed.slug), manifest

    async def _setup(self, job: Job, managed: children.Managed, install: bool) -> dict:
        try:
            installed = any(a.get("slug") == managed.slug for a in await self.sv.list_apps())
            if install:
                if installed:
                    raise JobFailed(f"{managed.slug} is installed already")
                expected, manifest = await asyncio.to_thread(self._definition_on_disk, managed)
                await self._wait_store(job, managed.slug, managed.marker["version"], manifest)
                await self._verify_store(job, managed, expected, manifest)
                job.log("installing")
                await self.sv.install(managed)
                await self._verify_installed(job, managed, expected)
            elif not installed:
                raise JobFailed(f"{managed.slug} is not installed: Install it")
            await self._finish_setup(job, managed)
        except Tampered as err:
            await self._uninstall_now(job, managed)
            await self._mark_tampered(managed.name, str(err))
            raise self._tampered(managed, str(err), "") from None
        except (SupervisorError, NotAllowed, RegistryError) as err:
            raise JobFailed(str(err)) from None
        info = await self._info(managed.slug) or {}
        return {"state": info.get("state", "started")}

    async def _delete(self, job: Job, managed: children.Managed, remove_data: bool) -> dict:
        try:
            apps = {a.get("slug"): a for a in await self.sv.list_apps()}
            app = apps.get(managed.slug)
            if app:
                if app.get("state") == "started":
                    job.log("stopping")
                    try:
                        await self.sv.stop(managed)
                    except SupervisorError as err:
                        job.log(f"stop: {err} (the uninstall stops it anyway)")
                job.log("uninstalling" + (" and deleting its data" if remove_data else " (its data folder stays)"))
                await self.sv.uninstall(managed, remove_config=remove_data)
            job.log(f"removing {names.folder_name(managed.name)}")
            await asyncio.to_thread(children.remove, managed)
            await asyncio.to_thread(self._forget, managed.name, managed.marker["instance_id"])
            await self.sv.reload_store()
        except (SupervisorError, NotAllowed, children.NotManaged, RegistryError) as err:
            raise JobFailed(str(err)) from None
        return {"removed": managed.slug, "data_removed": remove_data}

    async def _detached(self, job: Job, name: str) -> tuple[dict, dict]:
        """The registry's entry and the Supervisor's info of an instance whose definition is gone: in the registry,
        no folder, installed, detached after a store reload, no store entry, HRI's url.  JobFailed otherwise."""
        slug = names.supervisor_slug(name)
        try:
            entry = await asyncio.to_thread(self.registry.get, name)
        except RegistryError as err:
            raise JobFailed(str(err)) from None
        if entry is None:
            raise JobFailed(f"{name} is not in the manager's registry: not created by this manager, left alone")
        if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
            raise JobFailed(f"{names.folder_name(name)} exists: nothing to repair")
        try:
            if not any(a.get("slug") == slug for a in await self.sv.list_apps()):
                raise JobFailed(f"{slug} is not installed: nothing to repair")
            # only an app whose definition is gone from everywhere the store reads: after a reload, the Supervisor
            # calls it detached and its store has no app of that slug
            await self.sv.reload_store()
            info = await self.sv.app_info(slug)
            if info.get("detached") is not True:
                raise JobFailed(f"{slug} is not detached: a definition of it is in the local apps folder (in another "
                                f"folder than {names.folder_name(name)}?), so it is not the manager's to repair: left alone")
            if await self.sv.store_app(slug) is not None:
                raise JobFailed(f"the Supervisor's store has a definition of {slug}: not repaired, left alone")
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        if info.get("url") != names.HRI_URL:
            raise JobFailed(f"{slug} is not hass-remote-integration (its url is {info.get('url')!r}): left alone")
        return entry, info

    @staticmethod
    def installed_channel(info: dict) -> str | None:
        """The channel of the installed app, from the app itself: a local build of a 0.0.0-<12 hex> version is a git
        build, an image of an HRI release a release; anything else, None."""
        version = str(info.get("version") or "")
        if info.get("build") is True and names.GIT_VERSION_RE.fullmatch(version):
            return "git"
        if info.get("build") is False and names.parse_version(version) and names.supported_version(version):
            return "release"
        return None

    async def _repair(self, job: Job, name: str, user: str) -> dict:
        """Write the definition of an installed instance again, of its INSTALLED version on its INSTALLED channel and
        nothing else: a definition of another version would be offered as an update (and installed by the Supervisor
        on its own with auto_update on).  Only for an instance the registry holds.  When the exact source cannot be
        had, nothing is written: NeedsAttention, recorded in the registry and shown with the user's options."""
        try:
            return await self._repair_installed(job, name, user)
        except NeedsAttention as err:
            try:
                await asyncio.to_thread(self.registry.update, name,
                                        needs_attention={"reason": str(err), "at": children.now_iso()})
            except RegistryError as err2:
                _LOGGER.error("%s", err2)
            raise

    async def _repair_installed(self, job: Job, name: str, user: str) -> dict:
        slug = names.supervisor_slug(name)
        entry, info = await self._detached(job, name)
        version = str(info.get("version") or "")
        channel = self.installed_channel(info)
        if channel is None:
            raise NeedsAttention(f"the installed {slug} ({version or 'no version'}, "
                                 f"{'built on the device' if info.get('build') else 'an image'}) is neither an HRI "
                                 "release nor a git build of this manager")
        if channel != entry.get("channel"):
            raise NeedsAttention(f"the installed {slug} is a {'git build' if channel == 'git' else 'release'} "
                                 f"({version}), the manager's registry says {entry.get('channel')}: which source it "
                                 "was built from is not known")
        if channel == "git":
            try:
                ref = names.validate_ref(entry.get("ref_kind"), entry.get("ref"))
            except ValueError as err:
                raise NeedsAttention(f"the manager's registry has no usable branch or tag for {name}: {err}") from None
        else:
            ref = ("tag", f"v{version}")
        # the manager's own copy of the definition first: a release needs nothing from GitHub, a git instance only the
        # source of its installed commit
        copy = await self._usable_copy(job, name, entry, version)
        archive = None
        if channel == "release" and copy is not None:
            sha, source = copy.sha, copy.meta["template_source"]  # a source URL: copies.load checked it
            job.log(f"from the manager's copy of its definition (release {version}): nothing downloaded")
        elif channel == "release":
            try:
                release = await self.gh.release(f"v{version}")
            except GitHubError as err:
                raise JobFailed(f"the release v{version}: {err}") from None
            if release is None:
                raise NeedsAttention(f"HRI {version}, the installed version, is not a published release (any more)")
            recorded = entry.get("sha") if entry.get("version") == version else None
            try:
                archive, source = await self._fetch(job, "release", version, None, recorded=recorded)
            except TagMoved as err:
                await self._flag_moved(name, err)
                raise NeedsAttention(str(err)) from None
            sha = archive.sha
        else:
            # the commit the manager recorded for this instance, and only when it IS the installed version: never the
            # branch's head, never another commit
            recorded = [s for s in ((copy.sha if copy else None), entry.get("sha")) if isinstance(s, str)]
            sha = next((s for s in recorded if names.SHA_RE.fullmatch(s) and names.git_version(s) == version), None)
            if sha is None:
                raise NeedsAttention(f"the manager has no record of the commit of the installed {version} "
                                     f"(recorded: {', '.join(s[:12] for s in recorded) or 'none'})")
            if copy is not None and copy.sha != sha:
                copy = None  # the copy's config is of another commit
            job.log(f"checking that commit {sha[:12]}, the installed one, is on HRI's {ref[0]} {ref[1]}, and downloading it")
            try:
                archive, source = await self.gh.tarball_of_commit(sha, *ref)
            except NotHRICommit as err:
                raise NeedsAttention(f"{err}: not a commit of HRI's {ref[0]} {ref[1]}") from None
            except GitHubError as err:
                if "not found" in str(err):
                    raise NeedsAttention(f"commit {sha[:12]}, the installed one, cannot be downloaded: {err}") from None
                raise JobFailed(str(err)) from None
            except Exception as err:  # noqa: BLE001 - tarsafe.UnsafeArchive
                raise NeedsAttention(f"the archive of commit {sha[:12]} was refused: {err}") from None
        return await self._write_repaired(job, name, entry, channel, version, ref, sha, source, archive, copy, user)

    async def _write_repaired(self, job: Job, name: str, entry: dict, channel: str, version: str, ref: tuple[str, str],
                              sha: str | None, source: str, archive, copy: copies.Copy | None, user: str,
                              event: str = "repaired") -> dict:
        slug = names.supervisor_slug(name)
        marker = self._marker(name, channel, version, ref, sha, source, user, instance_id=entry.get("instance_id"))
        if copy is not None and isinstance(copy.meta.get("stamp_version"), int):
            marker["stamp_version"] = copy.meta["stamp_version"]  # the copy's config, as that manager stamped it
        marker["repaired_at"] = marker["updated_at"]
        marker["created_at"] = entry.get("created_at") or marker["created_at"]
        # the marker's history, from the registry (the folder is gone), and this write added to it
        marker["created_by"] = entry.get("created_by") or marker["created_by"]
        if entry.get("updated_by"):
            marker["updated_by"] = entry["updated_by"]
        history = self._history(entry.get("history"))
        history.append({**{k: entry.get(k) for k in ("channel", "version", "ref_kind", "ref", "sha", "updated_at")},
                        "event": f"{event} ({'automatic' if user == AUTO_USER else 'manual'}) at {marker['repaired_at']}",
                        "by": user})
        marker["history"] = self._history(history)
        setup_complete = entry.get("setup_complete", True)
        job.log(f"writing {names.folder_name(name)} again for {version}, the installed version")
        try:
            await asyncio.to_thread(self.registry.put, name, self._registry_entry(marker, setup_complete=setup_complete))
            managed = await asyncio.to_thread(
                children.write_new, self.root, name,
                self._builder(job, archive, channel, name, version, sha, marker, source, copy=copy), self.registry)
        except (stamp.TemplateError, children.UnsafePath, children.NotManaged, RegistryError, OSError, JobFailed) as err:
            # JobFailed: the builder's own refusal (more than one app)
            await asyncio.to_thread(self._restore_entry, name, entry, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        self._clear_auto(name, keep_job=job.id)  # an automatic repair's own note stays: it succeeded
        await self._save_copy(job, managed)
        await self._wait_store(job, slug, version)
        return {"slug": slug, "version": version}

    async def _update_detached(self, job: Job, name: str, version: str | None, ref: tuple[str, str] | None,
                               user: str) -> dict:
        """Update (release) or Rebuild (git) of an instance whose definition is gone and could not be written again at
        its installed version: the definition of a NEWER version, written and installed in one job the user asked
        for.  If the update does not succeed the definition is removed again (the instance stays detached)."""
        slug = names.supervisor_slug(name)
        entry, info = await self._detached(job, name)
        installed = str(info.get("version") or "")
        channel = entry.get("channel")
        if channel == "release":
            if version is None:
                try:
                    latest = latest_stable(await self.gh.releases())
                except GitHubError as err:
                    raise JobFailed(f"the release list: {err}") from None
                if not latest:
                    raise JobFailed("no stable HRI release found")
                version = latest["version"]
            if (names.parse_version(version) or ()) <= (names.parse_version(installed) or ()):
                raise JobFailed(f"{version} is not newer than the installed {installed}: the manager does not downgrade, "
                                "and the installed version is Repair's")
            await self._check_release(version)
            new_ref = ("tag", f"v{version}")
            archive, source = await self._fetch(job, "release", version, new_ref)
        else:
            try:
                new_ref = ref or names.validate_ref(entry.get("ref_kind"), entry.get("ref"))
            except ValueError as err:
                raise JobFailed(f"the manager's registry has no usable branch or tag for {name}: {err}; name one") from None
            archive, source = await self._fetch(job, "git", None, new_ref)
            version = names.git_version(archive.sha)
            if version == installed:
                if not isinstance(entry.get("needs_attention"), dict):
                    raise JobFailed(f"the {new_ref[0]} {new_ref[1]} is at the installed commit: Repair writes its definition")
                return await self._adopt_installed_commit(job, name, entry, info, new_ref, archive, source, user)
        sha = archive.sha
        # the registry's entry as the previous marker (the folder is gone): who created it, its history, this update
        marker = self._marker(name, channel, version, new_ref, sha, source, user,
                              previous={**entry, "history": self._history(entry.get("history"))},
                              instance_id=entry.get("instance_id"))
        marker["created_at"] = entry.get("created_at") or marker["created_at"]
        job.log(f"writing {names.folder_name(name)} for {version} (installed: {installed}), then updating")
        built: dict = {}
        try:
            await asyncio.to_thread(self.registry.put, name,
                                    self._registry_entry(marker, setup_complete=entry.get("setup_complete", True)))
            managed = await asyncio.to_thread(
                children.write_new, self.root, name,
                self._builder(job, archive, channel, name, version, sha, marker, source, built=built), self.registry)
        except (stamp.TemplateError, children.UnsafePath, children.NotManaged, RegistryError, OSError, JobFailed) as err:
            await asyncio.to_thread(self._restore_entry, name, entry, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        expected = stamp.expected_view(built["config"], slug)
        try:
            await self._wait_store(job, slug, version, managed.manifest)
            await self._verify_store(job, managed, expected, managed.manifest)
            job.log(f"updating {installed} -> {version}" + (" (building)" if channel == "git" else ""))
            await self.sv.update(managed)
            await self._verify_installed(job, managed, expected)
            after = await self.sv.app_info(slug)
            if after.get("version") != version:
                raise JobFailed(f"the Supervisor reports {after.get('version')} after the update, not {version}")
        except asyncio.CancelledError:
            job.log("the manager is stopping: removing the definition again")
            await self._shielded(job, self._undo_detached(job, managed, entry), "removing it")
            raise
        except Tampered as err:
            job.log(f"{err}: uninstalling it, and removing the definition again")
            await self._uninstall_now(job, managed)
            await self._undo_detached(job, managed, entry)
            await self._mark_tampered(name, str(err))
            raise self._tampered(managed, str(err), "and its definition removed again") from None
        except Exception as err:
            job.log(f"{err}: removing the definition again (the instance stays as it was)")
            await self._undo_detached(job, managed, entry)
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        self._clear_auto(name)
        await self._save_copy(job, managed)
        return {"version": version, "state": after.get("state")}

    async def _adopt_installed_commit(self, job: Job, name: str, entry: dict, info: dict, ref: tuple[str, str], archive,
                                      source: str, user: str) -> dict:
        """Rebuild of an instance that needs attention onto a branch or tag whose head IS the installed commit (a
        restore brought back another commit than the recorded one, so Repair refuses): the definition of that commit,
        the installed version, so the Supervisor has nothing to update; the registry then records that ref and commit.
        Repair's provenance rules hold: the installed app is a git build of that commit's version, and HRI's compare
        puts the commit on the ref."""
        sha, version = archive.sha, str(info.get("version") or "")
        if self.installed_channel(info) != "git" or names.git_version(sha) != version:
            raise JobFailed(f"the installed {names.supervisor_slug(name)} ({version}) is not a git build of commit "
                            f"{sha[:12]}")
        job.log(f"the {ref[0]} {ref[1]} is at {sha[:12]}, the installed commit: checking that it is on HRI's {ref[0]} "
                f"{ref[1]}, then writing its definition (nothing to update)")
        try:
            await self.gh.commit_on_ref(sha, *ref)
        except NotHRICommit as err:
            raise JobFailed(f"{err}: not a commit of HRI's {ref[0]} {ref[1]}") from None
        except GitHubError as err:
            raise JobFailed(str(err)) from None
        return await self._write_repaired(job, name, entry, "git", version, ref, sha, source, archive, None, user,
                                          event="rebuilt at the installed commit")

    async def _undo_detached(self, job: Job, managed: children.Managed, entry: dict) -> None:
        try:
            await asyncio.to_thread(children.remove, managed)
            await asyncio.to_thread(self.registry.put, managed.name, entry)
            await self.sv.reload_store()
        except Exception as err:  # noqa: BLE001
            job.log(f"could not undo: {err}")

    async def _delete_detached(self, job: Job, name: str, remove_data: bool, user: str) -> dict:
        """Delete of an instance whose definition is gone: a folder with the manager's marker only (no config.*: the
        store sees no app in it) gives the capability every changing call needs, then the ordinary delete."""
        entry, info = await self._detached(job, name)
        marker = self._marker(name, entry.get("channel"), str(info.get("version") or ""),
                              (entry.get("ref_kind") or "tag", entry.get("ref") or ""), entry.get("sha"), "delete", user,
                              instance_id=entry.get("instance_id"))
        try:
            managed = await asyncio.to_thread(children.write_new, self.root, name, lambda tmp: marker, self.registry)
        except (children.UnsafePath, children.NotManaged, OSError) as err:
            raise JobFailed(f"{name} cannot be deleted: {err}") from None
        try:
            return await self._delete(job, managed, remove_data)
        except BaseException:
            try:
                await asyncio.to_thread(children.remove, managed)
            except Exception as err:  # noqa: BLE001
                job.log(f"the marker folder was not removed: {err}")
            raise

    def _restore_entry(self, name: str, entry: dict | None, instance_id: str) -> None:
        if entry is not None:
            self.registry.put(name, entry)
        else:
            self._forget(name, instance_id)
