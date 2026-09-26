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
from .jobs import Busy, Job, JobFailed, Jobs, NeedsAttention, TagMoved, Tampered, Unverified
from .registry import Registry, RegistryError
from .supervisor import NotAllowed, SupervisorClient, SupervisorError

_LOGGER = logging.getLogger(__name__)
HISTORY = 20
# the Supervisor kills the manager 30 s after SIGTERM (config.yaml timeout), and the manager cancels its jobs before it
# waits for open requests (__main__.py): a rollback when the manager stops gets well within that
ROLLBACK_BOUND = 8.0
# containment (_contain: stop and uninstall an app installed from another definition) goes on for this long after the
# manager is told to stop; what it could not finish, the next start does again.  With the wait for open requests
# (__main__.SHUTDOWN_TIMEOUT) it fits in the stop timeout (config.yaml), tests/test_repo.py checks
CONTAIN_BOUND = 15.0
INTERRUPTED = "interrupted: the manager stopped before it finished; its next start tries again"
AUTO_REPAIR_INTERVAL = 300.0
AUTO_REPAIR_MAX_DELAY = 86400.0  # an instance's automatic repair that keeps failing is tried at least once a day
AUTO_USER = "automatic repair"
# an install the manager stopped waiting for, the Supervisor may finish later (it installs as a task of its own): its
# registry entry is not forgotten before the install call's own timeout has passed
INTERRUPTED_GRACE = 3600.0
# the background loop (run_background) started again after an error it did not expect: after this many seconds,
# doubling each time up to LOOP_RESTART_MAX
LOOP_RESTART_MIN = 5.0
LOOP_RESTART_MAX = 600.0
# a git definition is HRI's whole source tree in the local apps folder, which others can write, and a build runs its
# Dockerfile: it is installed only right after the manager downloaded and wrote it, never from what the folder holds
GIT_NOT_INSTALLED = ("{name} is a git build whose definition is not installed: the manager builds a git instance only "
                     "from a fresh download of its branch or tag, never from the tree left in the local apps folder. "
                     "Delete it (its /config folder is kept) and create it again from its branch or tag")


class InvalidRequest(ValueError):
    pass


# what writing a definition folder may raise, the builder's own refusals (JobFailed: more than one app; ValueError:
# stamp's) among them: every write path undoes its registry change on any of these
WRITE_ERRORS = (stamp.TemplateError, children.UnsafePath, children.NotManaged, children.DefinitionChanged, RegistryError,
                OSError, JobFailed, ValueError)


REGISTRY_FIELDS = ("name", "slug", "channel", "version", "ref_kind", "ref", "sha", "instance_id", "created_at", "updated_at",
                   "stamp_version", "created_by", "updated_by", "history", "bluetooth", "host_network")
# the accesses to the host an instance may be given, each the admin's choice per instance, recorded in the registry
# (and the marker) under its name: the key stamp() adds for it, which the Supervisor reports of the installed app too,
# the name the manager shows, and what it gives
ACCESS = {"bluetooth": ("host_dbus", "Bluetooth", "the host's D-Bus"),
          "host_network": ("host_network", "Host network", "the host's network")}
# an update applies an installed app's definition only when the app's version changes (an update at the same version
# is refused, AppNoUpdateAvailableError; apps/data.py copies the store's definition at install, update and rebuild).
# The Supervisor's own Rebuild applies it at the same version, but the manager's allow-list has no rebuild
NEEDS_VERSION = ("the Supervisor applies {label} ({what}) only when the app's version changes: "
                 "turn it {state} with an Update to a newer release, or a Rebuild once its branch or tag has a "
                 "new commit; or Delete the instance keeping its data and create it again with {label} {state}")
# the installed app's host_dbus (host_network) differs from the registry's Bluetooth (Host network), and nothing says
# the manager made that change (the Supervisor's own Update or Rebuild of a definition someone changed would): never
# recorded without the admin
NOT_CHOSEN = ("the installed {slug} {has} {what} ({label}), the manager's record says {record}, and "
              "the manager did not make that change (the Supervisor's own Update or Rebuild of a changed "
              "definition would): nothing was recorded. {choose}")
CHOOSE = ("Update it at its installed version with {label} {state} to keep that as your choice (the other "
          "needs a newer version), or Delete it")
CHOOSE_DETACHED = "Update it to a newer release (or Rebuild it) with {label} chosen, or Delete it"
# Host network only for an HRI that listens on the ingress port the Supervisor picks for it (stamp.stamp: ingress_port
# 0): an older one would listen on its own port (8087) on every interface of the host, where a second instance, or
# HRI's own app, would clash with it
HOST_NETWORK_RELEASE = ("Host network needs HRI {min} or newer, the first release that listens on the port the "
                        "Supervisor gives it: not {version}")
# a release from DYNAMIC_PORT_VERSION on whose archive does not set APP_DYNAMIC_PORT (stamp.reads_dynamic_port): the
# version alone is not taken for the code
HOST_NETWORK_RELEASE_CODE = ("the release {version} does not have the host network support the manager needs (its "
                             "entrypoint.py does not set APP_DYNAMIC_PORT = True): choose Host network off, or a "
                             "release that has it")
HOST_NETWORK_GIT = ("Host network needs an HRI that listens on the port the Supervisor gives it (its entrypoint.py "
                    "says APP_DYNAMIC_PORT = True, from 0.26.0 on), and commit {sha} of the {kind} {ref} does not: "
                    "Rebuild with Host network off, or from a branch or tag that has it")
# a definition follows the installed app in place (_follow_access) only where stamping can write it from the stamped
# one: turning Host network off needs HRI's own ingress_port back, which only its template has
FOLLOW_HOST_NETWORK_OFF = ("the installed {slug} does not have the host's network (Host network), the manager's record "
                           "says on, and its definition cannot follow it in place: {choose}")
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
        # name -> mark the registry could not record (a full /data): refused all the same while the manager runs, and
        # kept in the instance's marker for its next start (_contain)
        self._unrecorded_marks: dict[str, dict] = {}
        # what stopped the background loop last, while it waits to start again (run_background): shown by status()
        self.loop_problem: dict | None = None

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
        if self.loop_problem:
            out["problems"].append(f"the automatic check and repair stopped at {self.loop_problem['at']} "
                                   f"({self.loop_problem['error']}); it starts again in {self.loop_problem['delay']} s: "
                                   "the manager's log has the details")
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
        configs = await asyncio.to_thread(lambda: {n: self._config_sha256(n) for n, m, _ in folders if m is not None})
        found = await self._decoys()
        try:
            registered = await asyncio.to_thread(self.registry.all)
        except RegistryError as err:
            _LOGGER.error("%s", err)
            registered = {}
        for name, mark in self._unrecorded_marks.items():
            if name in registered and not isinstance(registered[name].get("tampered"), dict):
                registered[name] = {**registered[name], "tampered": mark}
        out, others, seen, repairable = [], [], set(), []
        for decoy in found:
            # the slug the Supervisor gives it: local_ (its local repository) and the one it declares
            others.append({"slug": f"local_{decoy.slug}" if decoy.slug else "", "name": decoy.path, "kind": "decoy",
                           "installed": False, "state": None, "problem": decoy.problem})
        for name, marker, problem in folders:
            slug = names.supervisor_slug(name)
            seen.add(slug)
            if marker is None:
                mark = (registered.get(name) or {}).get("tampered")
                if isinstance(mark, dict):  # the manager's own instance, whose marker a writer broke
                    problem = f"{str(mark.get('reason'))[:300]}: {self._mark_text(slug, mark)} ({problem})"
                others.append({"slug": slug, "name": names.folder_name(name), "kind": "folder", "problem": problem,
                               "installed": slug in installed, "state": installed.get(slug, {}).get("state")})
                continue
            entry = self._entry(name, marker, installed.get(slug), latest, registered.get(name))
            foreign = (self._foreign_version(registered.get(name), (installed.get(slug) or {}).get("version_latest"))
                       or self._foreign_config(registered.get(name), configs.get(name)))
            if foreign and not entry.get("job"):
                entry["problem"] = "; ".join(p for p in (entry.get("problem"), foreign) if p)
                if not isinstance((registered.get(name) or {}).get("tampered"), dict):  # a marked row keeps its own
                    entry["actions"] = ["repair"] + (["stop"] if entry["state"] == "started" else []) + ["delete"]
                entry["foreign"] = True
            decoyed = self._decoys_of(found, slug)
            if decoyed and not entry.get("job"):
                # nothing that makes the Supervisor install or update it while the store may take the decoy, nor start
                # it while one names it; one that names no instance leaves Start and Stop of the installed app
                entry["problem"] = "; ".join(p for p in (entry.get("problem"), self._decoy_text(decoyed)) if p)
                kept = ("stop", "delete") if any(d.slug for d in decoyed) else ("start", "restart", "stop", "delete")
                entry["actions"] = [a for a in entry["actions"] if a in kept]
            out.append(entry)
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
                    entry.update(self._access_of(known))
                    if isinstance(known.get("tampered"), dict):  # never repaired automatically; by hand when unverified
                        entry["problem"] = f"{str(known['tampered'].get('reason'))[:300]}: {self._mark_text(slug, known['tampered'])}"
                        entry["actions"] = (["repair"] if known["tampered"].get("unverified") else []) + ["delete"]
                        out.append(entry)
                        continue
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
                pending = self._install_may_finish(registered.get(name))
                if pending:
                    others.append({"slug": names.supervisor_slug(name), "name": name, "instance": name, "kind": "orphan",
                                   "installed": False, "state": None, "actions": [], "problem": pending})
                    continue
                others.append({"slug": names.supervisor_slug(name), "name": name, "instance": name, "kind": "orphan",
                               "installed": False, "state": None, "actions": ["forget"],
                               "problem": (f"{str(tampered.get('reason'))[:300]}: {self._mark_text(names.supervisor_slug(name), tampered)}. "
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
                # the badges show what the app has, not what the manager recorded; a difference is said
                for access, (key, label, what) in ACCESS.items():
                    has = info.get(key)
                    if not isinstance(has, bool) or has == entry[access]:
                        continue
                    known = registered.get(entry["name"])
                    choose = ("an update the manager made set it; its next Update or Repair records it"
                              if self._access_confirmed(known, access, has, None) else
                              "the manager did not make that change: " + (
                                  CHOOSE_DETACHED.format(label=label) if entry.get("detached")
                                  else CHOOSE.format(label=label, state="on" if has else "off")))
                    entry["problem"] = "; ".join(p for p in (entry.get("problem"), (
                        f"{label}: the installed app {'has' if has else 'does not have'} {what}, the manager's record "
                        f"says {'on' if entry[access] else 'off'}; {choose}")) if p)
                    entry[access] = has
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
                entry = self.registry.get(name) or {}
                files = copies.read_definition(children.child_path(self.root, name), marker.get("channel"))
                copies.save(self.copies_root, name, files, marker, **self._access_of(entry))
            except (copies.CopyError, children.UnsafePath, OSError, RegistryError) as err:
                _LOGGER.warning("the copy of %s's definition was not saved: %s", name, err)
                continue
            except Exception as err:  # noqa: BLE001 - one folder, whatever it holds, never stops the others
                _LOGGER.warning("the copy of %s's definition was not saved (%s): %s", name, type(err).__name__,
                                str(err)[:300], exc_info=True)
                continue
            done.append(name)
        return done

    async def startup(self) -> list[str]:
        """At the manager's start, before any job: what a crash or a stop left in the local apps folder is tidied
        (children.cleanup_stale), knowing which version the Supervisor has of each instance.  What was done."""
        try:
            installed: dict[str, str | None] | None = {}
            for app in await self.sv.list_apps():
                name = names.name_from_slug(app.get("slug"))
                if name:
                    installed[name] = app.get("version")
            notes = []
        except (SupervisorError, NotAllowed) as err:
            installed = None
            notes = [f"could not ask the Supervisor which apps are installed ({err}): an update that stopped midway is "
                     "left as it is until the next start"]
        notes += await asyncio.to_thread(children.cleanup_stale, self.root, self.registry, installed)
        notes += [n if n.startswith("could not") else f"the copies in /data: {n}"
                  for n in await asyncio.to_thread(copies.tidy, self.copies_root)]
        if installed is not None:
            follow, more = await asyncio.to_thread(self._marked_installed, installed)
            notes += more
            for name, managed, reason, pending in follow:
                if pending:
                    self.jobs.start(name, "check", AUTO_USER, lambda job, m=managed: self._recheck(job, m))
                    notes.append(f"{name}: installed while the manager was not watching: checking it")
                    continue
                self.jobs.start(name, "contain", AUTO_USER,
                                lambda job, m=managed, r=reason: self._contain_again(job, m, r))
                notes.append(f"{name}: installed from another definition than the manager's and still installed: "
                             "containing it again")
        return notes

    async def _recheck(self, job: Job, managed: children.Managed) -> dict:
        """An installed app the manager has not checked (marked pending: installed while it was not watching): its
        folder checked (copies.check) and the app compared with it; the mark goes when they agree, the app is
        contained when they differ, stopped and kept when the Supervisor does not report a field."""
        job.log("checking the installed app against its definition")
        try:
            try:
                expected, manifest = await asyncio.to_thread(self._definition_on_disk, managed)
            except JobFailed as err:
                # nothing to compare the running app with: not left running unchecked
                raise await self._hold(job, managed, reason=f"{err}; so the manager could not check the installed "
                                                            f"{managed.slug}") from None
            await self._check_tree(managed.slug, manifest)
            missing = await self._verify_installed(job, managed, expected)
        except Tampered as err:
            mark = await self._contain(job, managed, str(err))
            raise self._tampered(managed, str(err), mark, "") from None
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        if missing:
            raise await self._hold(job, managed, missing)
        decoyed = self._decoys_of(await self._decoys(), managed.slug)
        if decoyed:
            raise await self._hold(job, managed, reason=(
                f"{self._decoy_text(decoyed)}; the Supervisor may have installed {managed.slug} from it, so the "
                "manager could not check it"))
        await asyncio.to_thread(self.registry.update, managed.name, tampered=None)
        self._unrecorded_marks.pop(managed.name, None)
        await asyncio.to_thread(self._drop_trace, managed)
        await self._boot_back(job, managed, managed.entry.get("tampered"))
        return {"checked": managed.slug}

    def _marked_installed(self, installed: dict[str, str | None]) -> tuple[list, list[str]]:
        """At the start: the instances marked as installed from another definition whose app is still installed (a
        stop, a crash or a refusal cut their containment short): (name, Managed, reason) each, and notes."""
        try:
            registered = self.registry.all()
        except RegistryError as err:
            return [], [f"could not read the registry: {err}"]
        follow, notes = [], []
        for name, entry in sorted(registered.items()):
            mark = entry.get("tampered")
            if not isinstance(mark, dict) and name in installed:
                # a containment or hold whose mark the registry could not record kept it in the instance's marker:
                # recorded now (in memory when the registry still cannot be written), then followed as any mark
                try:
                    traced = children.load_managed(self.root, name, self.registry).marker.get("contained")
                except children.NotManaged:
                    traced = None
                mark = traced if isinstance(traced, dict) else None
                if mark is not None:
                    try:
                        self.registry.update(name, tampered=mark, setup_complete=False)
                    except RegistryError:
                        self._unrecorded_marks[name] = dict(mark)
                    notes.append(f"{name}: its mark, which the registry could not record, is taken from its marker")
            pending = isinstance(mark, dict) and mark.get("pending") is True
            if not isinstance(mark, dict) or (mark.get("unverified") and not pending) or name not in installed:
                continue
            try:
                managed = children.load_managed(self.root, name, self.registry)
            except children.NotManaged as err:  # the marker broken: the mark says what to do by hand
                notes.append(f"could not contain {name} again: {err}")
                continue
            follow.append((name, managed, str(mark.get("reason")), pending))
        return follow, notes

    async def _contain_again(self, job: Job, managed: children.Managed, reason: str) -> dict:
        job.log(f"still installed from another definition than the manager's ({reason[:200]}): containing it again")
        mark = await self._contain(job, managed, reason)
        if not mark["uninstalled"]:
            raise JobFailed(self._mark_text(managed.slug, mark))
        return {"uninstalled": managed.slug}

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
            self.loop_problem = None
            if self.auto_repair_interval is None:
                return
            await asyncio.sleep(self.auto_repair_interval)

    async def run_background(self) -> None:
        """auto_repair_loop, supervised (__main__ runs this as a task of its own, which nothing awaits): an error it
        does not expect is logged with its traceback and shown by the status, and the loop starts again after
        LOOP_RESTART_MIN seconds, doubling up to LOOP_RESTART_MAX, instead of ending unseen for the rest of the
        manager's life.  Cancelled when the manager stops."""
        delay = LOOP_RESTART_MIN
        while True:
            try:
                await self.auto_repair_loop()
                return
            except Exception as err:  # noqa: BLE001 - logged, shown, and the loop starts again
                self.loop_problem = {"error": f"{type(err).__name__}: {str(err)[:200]}", "at": children.now_iso(),
                                     "delay": round(delay)}
                _LOGGER.error("the automatic check and repair stopped (%s); it starts again in %d s", type(err).__name__,
                              delay, exc_info=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, LOOP_RESTART_MAX)

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
            **self._access_of(known),
        }
        if marker is None:
            return entry
        # the newer of the definition and the installed app: an update the manager stopped waiting for can leave the
        # app newer than its definition (as _update's downgrade check)
        current = max((marker.get("version"), app.get("version") if app else None), key=lambda v: names.parse_version(v) or ())
        if (marker.get("channel") == "release" and latest
                and (names.parse_version(latest["version"]) or ()) > (names.parse_version(current) or ())):
            entry["newer_release"] = latest["version"]
        actions, problems = [], []
        if known and known.get("tag_moved"):
            problems.append(str(known["tag_moved"])[:300])
        mark = known.get("tampered") if known else None
        if isinstance(mark, dict):
            # marked: stop (when it runs) and Delete only; nothing that starts, installs or rewrites it.  An app the
            # manager could not check may be checked again (Finish setup: checked, then set up and started)
            problems.append(f"{str(mark.get('reason'))[:300]}: {self._mark_text(slug, mark)}")
            entry["actions"] = (["finish"] if app and mark.get("unverified") else []) + (
                ["stop"] if app and app.get("state") == "started" else []) + ["delete"]
            entry["labels"] = {"finish": "Check again"}
            entry["problem"] = "; ".join(problems)
            return entry
        if app is None and known is not None and known.get("channel") == "git":
            problems.append(GIT_NOT_INSTALLED.format(name=name))
        elif app is None:
            problems.append("defined, but not installed: Install installs and starts it")
            actions.append("install")
        else:
            if known is not None and not known.get("setup_complete"):
                problems.append("its setup was interrupted: Finish setup turns on start at boot, the Watchdog and the "
                                "sidebar panel, and starts it")
                actions.append("finish")
            state = app.get("state")
            actions += ["restart", "stop"] if state == "started" else ["start"]
            actions.append("update")
        actions.append("delete")
        entry["actions"] = actions
        entry["problem"] = "; ".join(problems) or None
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

    @staticmethod
    def check_access(body: dict, default: bool | None) -> dict[str, bool | None]:
        """The request's choice of each access (ACCESS): true, false, or ``default`` when it names none."""
        out = {}
        for access in ACCESS:
            value = body.get(access, default)
            if value is not None and not isinstance(value, bool):
                raise InvalidRequest(f"{access} is true or false.")
            out[access] = value
        return out

    @staticmethod
    def _access_of(entry: dict | None) -> dict[str, bool]:
        """Each access as a registry entry (or a request's resolved choice) records it: on only when true."""
        return {access: (entry or {}).get(access) is True for access in ACCESS}

    def check_create(self, body: dict) -> tuple[str, str, str | None, tuple[str, str] | None, dict[str, bool]]:
        name = self.check_name(body.get("name"))
        channel = body.get("channel", "release")
        access = self.check_access(body, False)
        if channel == "release":
            version = body.get("version")
            if not isinstance(version, str) or not names.parse_version(version):
                raise InvalidRequest("Choose an HRI release.")
            if not names.supported_version(version):
                raise InvalidRequest("Instances need HRI 0.25.0 or newer (the first release that runs as an app).")
            if access["host_network"] and not names.reads_dynamic_port(version):
                raise InvalidRequest(HOST_NETWORK_RELEASE.format(min=self._dynamic_port_min(), version=version) + ".")
            return name, channel, version, None, access
        if channel == "git":
            return name, channel, None, self.check_ref(body.get("ref_kind"), body.get("ref")), access
        raise InvalidRequest("The channel is 'release' or 'git'.")

    @staticmethod
    def _dynamic_port_min() -> str:
        return ".".join(str(n) for n in names.DYNAMIC_PORT_VERSION)

    def _refuse_host_network(self, channel: str, version: str, archive, ref: tuple[str, str]) -> None:
        """JobFailed unless the HRI being written listens on the port the Supervisor gives it: its archive, the one the
        manager downloaded and stamps, has an entrypoint.py that says so (stamp.reads_dynamic_port, parsed, never run),
        and a release is names.DYNAMIC_PORT_VERSION or newer too.  Only for a definition written with Host network."""
        if channel == "release" and not names.reads_dynamic_port(version):
            raise JobFailed(HOST_NETWORK_RELEASE.format(min=self._dynamic_port_min(), version=version))
        try:
            supported = stamp.reads_dynamic_port(archive)
        except tarsafe.UnsafeArchive as err:
            raise JobFailed(f"the archive's {stamp.DYNAMIC_PORT_FILE}: {err}") from None
        if supported:
            return
        if channel == "release":
            raise JobFailed(HOST_NETWORK_RELEASE_CODE.format(version=version))
        raise JobFailed(HOST_NETWORK_GIT.format(sha=(archive.sha or "")[:12], kind=ref[0], ref=ref[1]))

    async def managed(self, name: str) -> children.Managed:
        """The instance's Managed, for a request: its marker and the registry (up to 4 MB) are read off the event loop."""
        name = self.check_name(name)
        try:
            return await asyncio.to_thread(children.load_managed, self.root, name, self.registry)
        except children.NotManaged as err:
            raise InvalidRequest(f"{name} is not an instance of this manager: {err}") from None

    # ------------------------------------------------------------------ jobs

    async def create(self, body: dict, user: str) -> Job:
        name, channel, version, ref, access = self.check_create(body)
        try:
            pending = self._install_may_finish(await asyncio.to_thread(self.registry.get, name))
        except RegistryError as err:
            raise InvalidRequest(str(err)) from None
        if pending:
            # a new entry would replace the one the install may still need: it would finish as an app of no instance
            raise InvalidRequest(f"{name}: {pending}")
        return self.jobs.start(name, "create", user,
                               lambda job: self._create(job, name, channel, version, ref, user, access))

    async def action(self, name: str, action: str, user: str) -> Job:
        managed = await self.managed(name)
        if action in ("start", "restart"):
            self._refuse_marked(name, managed.entry)
            await self._refuse_foreign(managed, starting=True)
        return self.jobs.start(name, action, user, lambda job: self._simple(job, managed, action))

    async def _detached_entry(self, name: str) -> dict | None:
        """The registry's entry of ``name`` when its folder is gone (a detached instance), else None."""
        name = self.check_name(name)

        def read() -> dict | None:
            if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
                return None
            return self.registry.get(name)

        try:
            return await asyncio.to_thread(read)
        except RegistryError as err:
            raise InvalidRequest(str(err)) from None

    async def update(self, name: str, body: dict, user: str) -> Job:
        detached = await self._detached_entry(name)
        channel = detached.get("channel") if detached else None
        if detached is None:
            managed = await self.managed(name)
            channel = managed.marker["channel"]
        self._refuse_marked(name, detached if detached is not None else managed.entry)
        if detached is None:
            await self._refuse_foreign(managed)
        version, ref = body.get("version"), None
        requested = self.check_access(body, None)  # None: as it is
        if channel == "release":
            if version is not None and (not isinstance(version, str) or not names.parse_version(version)):
                raise InvalidRequest("Choose an HRI release.")
        else:
            version = None
            if body.get("ref") is not None:
                ref = self.check_ref(body.get("ref_kind"), body.get("ref"))
        if detached is not None:
            # an instance Repair could not rewrite at its installed version: a newer one, written and installed at once
            return self.jobs.start(name, "update", user,
                                   lambda job: self._update_detached(job, name, version, ref, user, requested))
        return self.jobs.start(name, "update", user, lambda job: self._update(job, managed, version, ref, user, requested))

    async def delete(self, name: str, body: dict, user: str) -> Job:
        detached = await self._detached_entry(name)
        managed = await self.managed(name) if detached is None else None
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

    @staticmethod
    def _foreign_version(known: dict | None, offered: object) -> str | None:
        """Why the store's definition of a managed instance is not one the manager wrote: it offers a version the
        registry never recorded (nor an update of the manager's own names, in flight).  The Supervisor's own Update
        button, or its auto-update, would install that folder as it is, without the manager's checks.  None when it
        is the manager's."""
        if not known or not isinstance(offered, str):
            return None
        flag = known.get("updating")
        fields = flag.get("fields") if isinstance(flag, dict) and isinstance(flag.get("fields"), dict) else {}
        if offered in (known.get("version"), fields.get("version")):
            return None
        return (f"the store offers a definition the manager did not write ({offered[:40]}; the manager's is "
                f"{known.get('version')}): do not update or rebuild it from the Supervisor's app page. Someone changed "
                f"{names.folder_name(str(known.get('name')))}/ in the local apps folder; Repair writes the manager's "
                "definition again, Delete removes the instance")

    @staticmethod
    def _foreign_config(known: dict | None, found: str | None) -> str | None:
        """Why the folder's config.yaml is not the one the manager wrote, at any version: its sha256 (``found``; None:
        it cannot be read) is neither the registry's (``config_sha256``) nor that of an update in flight.  The
        Supervisor's own Rebuild applies a definition of the installed version as it is, without the manager's checks.
        None when it is the manager's, or when the registry has no sha256 to compare (last written by 0.1.2)."""
        if not known:
            return None
        flag = known.get("updating")
        fields = flag.get("fields") if isinstance(flag, dict) and isinstance(flag.get("fields"), dict) else {}
        recorded = [h for h in (known.get("config_sha256"), fields.get("config_sha256")) if isinstance(h, str)]
        if not recorded or found in recorded:
            return None
        folder = names.folder_name(str(known.get("name")))
        return (f"{folder}/config.yaml is not the one the manager wrote for {known.get('version')} (someone changed it "
                "in the local apps folder): do not update or rebuild it from the Supervisor's app page, which would "
                "apply it. Repair writes the manager's definition again, Delete removes the instance")

    def _config_sha256(self, name: str) -> str | None:
        """The sha256 of ``hri_<name>/config.yaml`` as it is now (never through a link), None when it cannot be read."""
        try:
            return hashlib.sha256(children.read_file(children.child_path(self.root, name), "config.yaml")).hexdigest()
        except (OSError, children.UnsafePath, names.InvalidName):
            return None

    def _record_config(self, name: str):
        """A builder's on_built: the registry records the sha256 of the config.yaml written for ``name``."""
        def record(digest: str) -> None:
            self.registry.update(name, config_sha256=digest)
        return record

    async def _refuse_foreign(self, managed: children.Managed, starting: bool = False) -> None:
        """InvalidRequest while the store offers a definition of the instance the manager did not write, or the local
        apps folder holds a decoy of it (stamp.decoys) that the store may take instead.  ``starting`` (Start, Restart
        of the installed app, which installs nothing): only a decoy that names the instance; one that names none (a
        file the manager cannot read, a folder it cannot search) refuses installs and updates, not that."""
        found = self._decoys_of(await self._decoys(), managed.slug)
        if starting:
            found = [d for d in found if d.slug is not None]
        if found:
            raise InvalidRequest(f"{managed.name}: {self._decoy_text(found)}. Nothing is installed, updated or started "
                                 "while it is there")
        try:
            offered = (await self.sv.store_app(managed.slug) or {}).get("version_latest")
        except (SupervisorError, NotAllowed) as err:
            raise InvalidRequest(str(err)) from None
        found_config = await asyncio.to_thread(self._config_sha256, managed.name)
        foreign = self._foreign_version(managed.entry, offered) or self._foreign_config(managed.entry, found_config)
        if foreign:
            raise InvalidRequest(f"{managed.name}: {foreign}")

    async def repair(self, name: str, user: str) -> Job:
        name = self.check_name(name)
        try:
            known = await asyncio.to_thread(self.registry.get, name)
        except RegistryError as err:
            raise InvalidRequest(str(err)) from None
        if known is None:
            raise InvalidRequest(f"{name} is not in the manager's registry: not created by this manager, so it is not "
                                 "repaired")
        self._refuse_marked(name, known, check=True)
        if await asyncio.to_thread(os.path.lexists, os.path.join(self.root, names.folder_name(name))):
            # its folder is there: Repair only when it holds a definition the manager did not write
            managed = await self.managed(name)
            try:
                offered = (await self.sv.store_app(managed.slug) or {}).get("version_latest")
            except (SupervisorError, NotAllowed) as err:
                raise InvalidRequest(str(err)) from None
            found_config = await asyncio.to_thread(self._config_sha256, name)
            if not (self._foreign_version(managed.entry, offered) or self._foreign_config(managed.entry, found_config)):
                raise InvalidRequest(f"{names.folder_name(name)} exists and holds the manager's definition: nothing to "
                                     "repair")
            return self.jobs.start(name, "repair", user, lambda job: self._repair_foreign(job, managed, user))
        return self.jobs.start(name, "repair", user, lambda job: self._repair(job, name, user))

    async def _repair_foreign(self, job: Job, managed: children.Managed, user: str) -> dict:
        """The store offers a definition the manager did not write: the folder goes (out of the store's sight), and
        Repair writes the manager's definition of the installed version again, and checks the installed app."""
        await self._refuse_marked_now(managed.name, check=True)
        await self._refuse_decoys()  # before the folder is removed and the store reloaded
        job.log(f"{names.folder_name(managed.name)} holds a definition the manager did not write: removing it")
        try:
            await asyncio.to_thread(children.remove, managed)
            await self.sv.reload_store()
        except (SupervisorError, NotAllowed, children.NotManaged, OSError) as err:
            raise JobFailed(str(err)) from None
        return await self._repair(job, managed.name, user)

    @staticmethod
    def _install_may_finish(entry: dict | None) -> str | None:
        """Why an interrupted install may still finish (within INTERRUPTED_GRACE of its interruption), else None."""
        at = (entry or {}).get("interrupted_at") if (entry or {}).get("interrupted") else None
        try:
            since = time.time() - datetime.datetime.fromisoformat(at).timestamp() if isinstance(at, str) else None
        except ValueError:
            since = None
        if since is None or since >= INTERRUPTED_GRACE:
            return None
        return (f"its install was interrupted at {at} and the Supervisor may still finish it: Forget is refused for "
                f"{int(INTERRUPTED_GRACE // 60)} minutes after that (then, or once it is listed as installed, Repair "
                "or Delete)")

    async def forget(self, name: str, body: dict, user: str) -> Job:
        """Drop the registry entry and the copy of an instance that is neither installed nor defined (checked again in
        the job).  Needs the name typed."""
        name = self.check_name(name)
        if body.get("confirm") != name:
            raise InvalidRequest(f"Forgetting an instance needs its name typed: {name}.")
        if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
            raise InvalidRequest(f"{names.folder_name(name)} exists: {name} is defined, not something to forget")
        try:
            pending = self._install_may_finish(await asyncio.to_thread(self.registry.get, name))
        except RegistryError as err:
            raise InvalidRequest(str(err)) from None
        if pending:
            raise InvalidRequest(f"{name}: {pending}")
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
            pending = self._install_may_finish(known)
            if pending:
                raise JobFailed(f"not forgotten: {pending}")
            if known is not None:
                await asyncio.to_thread(self.registry.remove, name)
            await asyncio.to_thread(copies.remove, self.copies_root, name)
        except (RegistryError, OSError) as err:
            raise JobFailed(str(err)) from None
        self._clear_auto(name)
        job.log(f"forgot {name}: " + ("its registry entry and " if known else "") + "any copy of its definition")
        return {"forgotten": name}

    async def setup(self, name: str, action: str, user: str) -> Job:
        """``install`` (a definition without its app) or ``finish`` (installed, but its create stopped before the
        options and the start)."""
        managed = await self.managed(name)
        self._refuse_marked(name, managed.entry, check=action == "finish")
        await self._refuse_foreign(managed)
        if action == "install" and managed.entry.get("channel") == "git":
            raise InvalidRequest(GIT_NOT_INSTALLED.format(name=name))
        return self.jobs.start(name, action, user, lambda job: self._setup(job, managed, install=action == "install"))

    def pending_setup(self) -> list[str]:
        """Instances whose create stopped between the install and the start (logged when the manager starts)."""
        try:
            return sorted(n for n, e in self.registry.all().items() if not e.get("setup_complete") and not e.get("interrupted")
                          and os.path.isdir(os.path.join(self.root, names.folder_name(n))))
        except RegistryError:
            return []

    # ------------------------------------------------------------------ job bodies

    async def _check_tree(self, slug: str, manifest: dict | None, installed: bool = False) -> None:
        """JobFailed when the definition folder is no longer what the manager wrote (``manifest``, None: not checked);
        Tampered when that is found right after an install or update (``installed``): the Supervisor may have
        installed the changed definition."""
        if manifest is None:
            return
        try:
            await asyncio.to_thread(children.check_tree, self.root, names.name_from_slug(slug), manifest)
        except children.DefinitionChanged as err:
            if installed:
                raise Tampered(f"{err}, around the install or update: the Supervisor may have installed the changed "
                               "definition") from None
            message = (f"{err}: refused, nothing installed or updated. Anyone who can write the local apps folder (the "
                       "addons share, SSH, another app that maps it) can change a definition; find out who did")
            _LOGGER.error("%s", message)
            raise JobFailed(message) from None

    async def _decoys(self) -> list[stamp.Decoy]:
        """stamp.decoys of the local apps folder, read off the event loop.  A folder that cannot be searched (tried
        twice: what changed in between may have been another job's own) is a decoy of every instance (slug None,
        ``scan_failed``) whose problem says why and what to do: nothing is installed or updated then, but it is never
        taken for a decoy found (_check_installed_source holds, never contains, for it)."""
        for attempt in (1, 2):
            try:
                return await asyncio.to_thread(stamp.decoys, self.root)
            except stamp.ScanError as err:
                failure = str(err)
        return [stamp.Decoy(".", None, f"{failure}: move or remove what is too deep or too large there, or fix its "
                                       "permissions", scan_failed=True)]

    @staticmethod
    def _decoys_of(found: list[stamp.Decoy], slug: str) -> list[stamp.Decoy]:
        """The decoys that may be taken for the definition of the instance ``slug`` (local_hri_<name>): those declaring
        its slug (compared as stamp.in_manager_space does), and those the manager could not read."""
        key = names.host_key(names.config_slug(slug[len(names.SLUG_PREFIX):]))
        return [d for d in found if d.slug is None or names.host_key(d.slug) == key]

    @staticmethod
    def _decoy_text(found: list[stamp.Decoy]) -> str:
        return "; ".join(d.problem for d in found[:3]) + (f"; and {len(found) - 3} more" if len(found) > 3 else "")

    async def _refuse_decoys(self) -> None:
        """JobFailed while the local apps folder holds a decoy of any instance, or cannot be searched (stamp.decoys: a
        store reload makes the Supervisor read it, and an install or update may take it).  Right after an install or
        update, _check_installed_source decides instead."""
        found = await self._decoys()
        if not found:
            return
        message = f"{self._decoy_text(found)}: refused, nothing installed or updated"
        _LOGGER.error("%s", message)
        raise JobFailed(message)

    async def _check_installed_source(self, managed: children.Managed, manifest: dict | None) -> str | None:
        """Right after an install or update, before the installed app is compared with the definition: the folder is
        still what the manager wrote, and no decoy has appeared; either may be what the Supervisor installed (a store
        reload by anyone, the Supervisor's own every 3 hours among them, can come between the last check and the
        install).  Tampered for a decoy of this instance's slug.  For one that names none (the manager cannot read
        it, or could not search the folder): why the app must be held, not contained (nothing of it was found), for the
        caller; as the start's check does (_recheck).  A decoy of another instance is not this one's: the next install
        or update refuses while it is there."""
        await self._check_tree(managed.slug, manifest, installed=True)
        found = self._decoys_of(await self._decoys(), managed.slug)
        named = [d for d in found if d.slug is not None]
        if named:
            raise Tampered(f"after the install or update, {self._decoy_text(named)}")
        if found:
            return (f"after the install or update, {self._decoy_text(found)}; so the manager could not check that the "
                    f"Supervisor installed {managed.slug} from its definition")
        return None

    @staticmethod
    def _future_text(future: tuple[str, float]) -> str:
        when = datetime.datetime.fromtimestamp(future[1], datetime.timezone.utc).replace(microsecond=0).isoformat()
        return (f"{tarsafe.show(future[0])} in the local apps folder is dated {when}, in the future, and the Supervisor "
                "notices changes there only by a newer date. Give it the current date (touch it) and try again")

    async def _reload_fresh(self) -> None:
        """A store reload that reads the local apps folder again: refused (JobFailed) while an entry of the folder is
        dated in the future, which would hide the change; otherwise the folder's own time is made the newest
        (children.force_reread), or the Supervisor keeps what it read before, a decoy removed since among it."""
        future = await asyncio.to_thread(children.newest_future, self.root)
        if future:
            raise JobFailed(f"refused, nothing installed or updated: {self._future_text(future)}")
        try:
            await asyncio.to_thread(children.force_reread, self.root)
        except (OSError, children.UnsafePath) as err:
            raise JobFailed(f"the local apps folder could not be marked to be read again ({err}): refused, nothing "
                            "installed or updated") from None
        await self.sv.reload_store()

    async def _wait_store(self, job: Job, slug: str, version: str, manifest: dict | None = None) -> None:
        """Reload the store until it has ``slug`` at ``version``.  ``manifest``: what the manager wrote, checked
        again right before each reload (the store reads the folder then), and that no other folder holds a decoy.
        Each reload reads the folder again (_reload_fresh)."""
        await self._check_tree(slug, manifest)
        await self._refuse_decoys()
        job.log("reloading the Supervisor's store")
        await self._reload_fresh()
        # by the clock: a store answer can take up to its own timeout, which counting sleeps would not see
        start = time.monotonic()
        reloaded_again = False
        while True:
            entry = await self.sv.store_app(slug)
            if entry and entry.get("version_latest") == version:
                job.log(f"the store has {slug} {version}")
                return
            waited = time.monotonic() - start
            if waited >= self.store_timeout:
                message = f"the Supervisor's store did not show {slug} {version} within {int(self.store_timeout)} s"
                future = await asyncio.to_thread(children.newest_future, self.root)
                if future:
                    message += f": {self._future_text(future)}"
                raise JobFailed(message)
            if not reloaded_again and waited >= self.store_timeout / 2:
                await self._check_tree(slug, manifest)
                await self._refuse_decoys()
                await self._reload_fresh()
                reloaded_again = True
            await asyncio.sleep(self.poll_interval)

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
        job.keep(archive)  # a spooled file of up to 256 MB: closed after its build, or at the latest when the job ends
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
                previous: dict | None = None, instance_id: str | None = None, access: dict | None = None) -> dict:
        now = children.now_iso()
        marker = {
            "manager": children.MANAGER_ID, "manager_version": VERSION, "name": name, "slug": names.supervisor_slug(name),
            "channel": channel, "version": version, "ref_kind": ref[0], "ref": ref[1], "sha": sha, **self._access_of(access),
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

    async def _undo_registry(self, job: Job, fn, *args) -> None:
        """A registry change undone after a refused write, best effort: its own failure (a full /data) is logged and
        never replaces the error the job reports."""
        try:
            await asyncio.to_thread(fn, *args)
        except RegistryError as err:
            job.log(f"the manager's registry was not put back: {err}")
            _LOGGER.error("%s", err)

    def _forget(self, name: str, instance_id: str) -> None:
        """Drop the registry's entry of ``name``, and the copy of its definition, when it is still that instance."""
        entry = self.registry.get(name)
        if entry and entry.get("instance_id") == instance_id:
            self.registry.remove(name)
            self._unrecorded_marks.pop(name, None)
            self._clear_auto(name)
            try:
                copies.remove(self.copies_root, name)
            except OSError as err:
                _LOGGER.warning("the copy of %s's definition was not removed: %s", name, err)

    async def _save_copy(self, job: Job, managed: children.Managed, access: dict, files: dict[str, bytes]) -> None:
        """Keep a copy of the definition just written in /data (copies.py), from ``files``, the bytes the build wrote
        (_builder's "files"): never read back from the folder, which others can write.  A failure costs only the
        offline Repair: the job goes on.  ``access``: the instance's choices, as the registry records them."""
        try:
            saved = await asyncio.to_thread(lambda: copies.save(self.copies_root, managed.name, files, managed.marker,
                                                                **self._access_of(access)))
        except Exception as err:  # noqa: BLE001 - a copy that fails, however, costs only the offline Repair
            job.log(f"no copy of the definition kept in the manager's /data ({type(err).__name__}: {str(err)[:300]}): "
                    "a Repair would download it")
            _LOGGER.warning("the copy of %s's definition was not saved: %s", managed.name, err,
                            exc_info=not isinstance(err, (copies.CopyError, children.UnsafePath, OSError)))
            return
        job.log(f"a copy of the definition is kept in the manager's /data ({len(saved)} file(s))")

    async def _usable_copy(self, job: Job, name: str, entry: dict, version: str) -> copies.Copy | None:
        try:
            return await asyncio.to_thread(copies.load, self.copies_root, name, entry, version)
        except (copies.CopyError, names.InvalidName, OSError) as err:
            job.log(f"the manager's copy of the definition is not used: {err}")
            return None

    def _builder(self, job: Job, archive, channel: str, name: str, version: str, sha: str | None, marker: dict, source: str,
                 copy: copies.Copy | None = None, built: dict | None = None, access: dict | None = None,
                 on_built=None):
        """``copy``: Repair from the manager's copy, of a release (its files as they are: no archive) or of a git
        instance (its stamped config over the archive of its commit).  ``built``: gets the stamped config written, as
        "config" (what the Supervisor is then checked to report), and the bytes of the files a copy keeps, as "files".
        ``on_built(sha256 of config.yaml)``: called once the folder is built, before it is put in place (the registry
        records it: _foreign_config).  ``access``: the instance's choices (ACCESS) the definition is stamped with."""
        chosen = self._access_of(access)

        def build(tmp: str) -> dict:
            try:
                return fill(tmp)
            finally:
                if archive is not None:
                    archive.close()  # read whole by now

        def fill(tmp: str) -> dict:
            extras: dict[str, bytes] = {}
            if copy is not None and channel == "release":
                # the config dumped by the manager from the checked mapping, never the copy's bytes
                config = copy.config
                children.write_file(tmp, "config.yaml", stamp.dump(config, source))
                extras = {rel: data for rel, data in copy.files.items() if rel != "config.yaml"}
                for rel, data in sorted(extras.items()):
                    children.write_file(tmp, rel, data)
            elif channel == "release":
                config = stamp.build_release(archive, tmp, name, version, source, **chosen)
                extras = stamp.app_extras(archive)
            else:
                config, notes = stamp.build_git(archive, tmp, name, version, sha, source,
                                                config=copy.config if copy is not None else None, **chosen)
                for note in notes:
                    job.log(note)
            data = stamp.dump(config, source)  # the bytes the builders wrote as config.yaml
            if built is not None:
                built["config"] = config
                built["files"] = {"config.yaml": data, **extras}
            found = stamp.find_configs(tmp)
            if found != ["config.yaml"]:
                raise JobFailed(f"the definition would hold more than one app: {found}")
            if on_built is not None:
                on_built(hashlib.sha256(data).hexdigest())
            return marker
        return build

    async def _create(self, job: Job, name: str, channel: str, version: str | None, ref: tuple[str, str] | None,
                      user: str, access: dict | None = None) -> dict:
        slug = names.supervisor_slug(name)
        access = self._access_of(access)
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
        await self._refuse_decoys()
        try:
            pending = self._install_may_finish(await asyncio.to_thread(self.registry.get, name))
        except RegistryError as err:
            raise JobFailed(str(err)) from None
        if pending:
            raise JobFailed(f"{name}: {pending}")
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
        if access["host_network"]:
            self._refuse_host_network(channel, version, archive, ref)
        marker = self._marker(name, channel, version, ("tag", f"v{version}") if channel == "release" else ref, sha, source, user,
                              access=access)
        for chosen, (key, label, what) in ACCESS.items():
            if access[chosen]:
                job.log(f"with {label}: the instance gets {what} ({key})")
        job.log(f"writing {names.folder_name(name)}")
        built: dict = {}
        try:
            await asyncio.to_thread(self.registry.put, name, self._registry_entry(marker))
            managed = await asyncio.to_thread(children.write_new, self.root, name,
                                              self._builder(job, archive, channel, name, version, sha, marker, source,
                                                            built=built, access=access,
                                                            on_built=self._record_config(name)),
                                              self.registry)
        except WRITE_ERRORS as err:
            await self._undo_registry(job, self._forget, name, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        await self._save_copy(job, managed, access, built["files"])
        expected = stamp.expected_view(built["config"], slug)
        installing = False
        try:
            await self._wait_store(job, slug, version, managed.manifest)
            await self._verify_store(job, managed, expected, managed.manifest)
            job.log("installing (a git build takes several minutes)" if channel == "git" else "installing (pulling the image)")
            installing = True
            await self.sv.install(managed)
            installing = False
            unsearched = await self._check_installed_source(managed, managed.manifest)
            missing = await self._verify_installed(job, managed, expected)
            if missing or unsearched:
                raise await self._hold(job, managed, missing, reason=unsearched)
            await self._finish_setup(job, managed)
        except asyncio.CancelledError:
            job.log("the manager is stopping: rolling back")
            await self._shielded(job, self._rollback_create(job, managed, interrupted=True), "the rollback")
            raise
        except Unverified:
            raise  # installed and kept: not rolled back
        except Tampered as err:
            mark = await self._contain(job, managed, str(err))
            if mark["uninstalled"]:
                try:
                    await asyncio.to_thread(children.remove, managed)
                    await self.sv.reload_store()
                except Exception as err2:  # noqa: BLE001 - reported; the mark stands
                    job.log(f"the definition was not removed: {err2}")
            raise self._tampered(managed, str(err), mark, "and its definition removed") from None
        except Exception as err:
            job.log(f"{err}: rolling back")
            # no clean refusal (a timeout, a lost connection, a server error): the Supervisor may be installing still
            unsure = installing and isinstance(err, SupervisorError) and not (err.status and 400 <= err.status < 500)
            left = await self._rollback_create(job, managed, interrupted=unsure)
            if left:
                raise JobFailed(f"{err}. {left}") from None
            if unsure:
                raise JobFailed(f"{err}. The Supervisor may still be installing it: if it finishes, the list shows "
                                f"{slug} as install interrupted, with Repair") from None
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        # the instance is up: a failed read of its info no longer undoes it
        info = await self._info(slug) or {}
        return {"slug": slug, "version": version, "ingress_url": info.get("ingress_url"), "state": info.get("state", "started")}

    async def _finish_setup(self, job: Job, managed: children.Managed, boot: bool = True) -> None:
        """Start at boot (unless ``boot`` is False: the admin had turned it off), the Watchdog and the panel on, and
        the app started; the setup recorded complete, and any mark cleared."""
        job.log(("turning on start at boot, the Watchdog and the sidebar panel" if boot else
                 "turning on the Watchdog and the sidebar panel (start at boot stays off, as it was)"))
        options = {"boot": "auto", "watchdog": True, "ingress_panel": True} if boot else {"watchdog": True,
                                                                                          "ingress_panel": True}
        await self.sv.set_options(managed, **options)
        if (await self.sv.app_info(managed.slug)).get("state") != "started":
            job.log("starting")
            await self.sv.start(managed)
        await asyncio.to_thread(self.registry.update, managed.name, setup_complete=True, tampered=None)
        self._unrecorded_marks.pop(managed.name, None)  # a mark only memory held goes with the registry's
        await asyncio.to_thread(self._drop_trace, managed)

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
        if problems and all(p.endswith(" not reported") for p in problems):
            message = (f"the Supervisor's store does not report {', '.join(p.split()[0] for p in problems)} of "
                       f"{managed.slug}, so the manager cannot check its definition (a change of the Supervisor's "
                       "API?): refused, nothing installed or updated; a newer manager may read it")
            _LOGGER.error("%s", message)
            raise JobFailed(message)
        if problems:
            message = (f"the Supervisor's store holds another definition of {managed.slug} than the one the manager "
                       f"wrote ({'; '.join(problems)}): refused, nothing installed or updated. Someone changed "
                       f"{names.folder_name(managed.name)}/ in the local apps folder after the manager wrote it (the "
                       "addons share, SSH, another app that maps it); find out who did")
            _LOGGER.error("%s", message)
            raise JobFailed(message)
        await self._check_tree(managed.slug, manifest)
        await self._refuse_decoys()
        job.log("the store's definition is the one the manager wrote")

    async def _verify_installed(self, job: Job, managed: children.Managed, expected: dict) -> list[str]:
        """Right after an install or update: the installed app reports what the manager stamped.  Tampered for a
        field that differs (the caller contains it); the fields the Supervisor did not report at all, returned: that
        is a change of its API, not tampering, and the caller stops and marks the app without uninstalling it."""
        view = await self.sv.app_definition(managed.slug)
        problems = stamp.view_differences(view, expected, stamp.INSTALLED_VIEW)
        missing = [key for key in stamp.INSTALLED_VIEW if key not in view]
        changed = [p for p in problems if not p.endswith(" not reported")]
        if changed:
            raise Tampered(f"the Supervisor installed another definition of {managed.slug} than the one the manager "
                           f"wrote ({'; '.join(changed)})")
        if not missing:
            job.log("the installed definition is the one the manager wrote")
        return missing

    async def _hold(self, job: Job, managed: children.Managed, missing: list[str] | None = None,
                    reason: str | None = None) -> Unverified:
        """An installed app the manager could not check (the fields the Supervisor did not report, ``missing``, or
        ``reason``): marked, stopped and its start at boot turned off (through the marker gate: the Supervisor starts
        every app with boot auto at its next start), kept installed with its options and data.  Clearing the mark gives
        start at boot back (_finish_setup, _boot_back).  The error for the job."""
        mark = {"reason": reason or (f"the Supervisor does not report {', '.join(missing or [])} of the installed "
                                     f"{managed.slug}, so the manager could not check it (a change of the Supervisor's "
                                     "API?)"),
                "at": children.now_iso(), "unverified": True, "uninstalled": False, "stopped": False, "failure": None}
        await self._set_mark(managed.name, mark)
        boot = None
        try:
            info = await self.sv.app_info(managed.slug)
            boot = info.get("boot")
            if info.get("state") == "started":
                job.log("stopping it")
                await self.sv.stop(managed)
            mark["stopped"] = True
        except Exception as err:  # noqa: BLE001 - reported in the mark and the job
            mark["failure"] = str(err) or err.__class__.__name__
        await self._boot_off(job, managed, mark, boot, "start at boot turned off until it is checked")
        if not await self._set_mark(managed.name, mark):
            await asyncio.to_thread(self._trace_mark, managed, dict(mark))
        message = f"{mark['reason']}: {self._mark_text(managed.slug, mark)}"
        _LOGGER.error("%s", message)
        return Unverified(message)

    async def _boot_off(self, job: Job, managed: children.Managed, mark: dict, boot: object, done: str) -> None:
        """Start at boot off for an app the manager holds or contains (the Supervisor starts every app with boot auto
        at its next start), through the marker gate.  ``boot``: the app's before (None: not known).  Recorded in
        ``mark["boot_manual"]``: True turned off by the manager (and given back when the mark is cleared), False it
        could not be, None it was off already, the admin's choice, which clearing the mark keeps."""
        if boot == "manual":
            mark["boot_manual"] = None
            job.log("start at boot was off already: left so")
            return
        try:
            await self.sv.set_options(managed, boot="manual")
            mark["boot_manual"] = True
            job.log(done)
        except Exception as err:  # noqa: BLE001 - reported in the mark and the job
            mark["boot_manual"] = False
            job.log(f"start at boot NOT turned off: {err}")

    async def _boot_back(self, job: Job, managed: children.Managed, mark: object) -> None:
        """A hold cleared (its check passed) that had turned start at boot off: on again (Check again does it itself,
        _finish_setup).  A failure is said, never the job's: the app is checked."""
        if not (isinstance(mark, dict) and mark.get("boot_manual") is True):
            return
        try:
            await self.sv.set_options(managed, boot="auto")
            job.log("start at boot turned on again")
        except (SupervisorError, NotAllowed) as err:
            job.log(f"start at boot NOT turned on again ({err}): turn it on in Settings > Apps")

    async def _set_mark(self, name: str, mark: dict) -> bool:
        """The registry records ``mark``; when it cannot, the manager keeps it in memory (refused all the same:
        _refuse_marked) and says so.  Whether the registry recorded it."""
        try:
            await asyncio.to_thread(self.registry.update, name, tampered=mark, setup_complete=False)
        except RegistryError as err:
            self._unrecorded_marks[name] = dict(mark)
            _LOGGER.error("%s: its mark is kept in memory only: %s", name, err)
            return False
        self._unrecorded_marks.pop(name, None)
        return True

    def _drop_trace(self, managed: children.Managed) -> None:
        """The mark cleared: its trace in the instance's marker goes too, or the next start would take it again.
        Best effort, logged."""
        if "contained" not in managed.marker:
            return
        try:
            children.replace_file(children.child_path(self.root, managed.name), children.MARKER, children.marker_bytes(
                {k: v for k, v in managed.marker.items() if k != "contained"}))
        except (OSError, children.UnsafePath) as err:
            _LOGGER.error("%s: the trace of its mark was not removed from its marker: %s", managed.name, err)

    def _trace_mark(self, managed: children.Managed, mark: dict) -> None:
        """A mark the registry could not record, kept in the instance's marker (``contained``) for the manager's next
        start, which contains the app again (_marked_installed).  Best effort: logged when it cannot be written."""
        try:
            folder = children.child_path(self.root, managed.name)
            children.replace_file(folder, children.MARKER, children.marker_bytes({**managed.marker, "contained": mark}))
        except (OSError, children.UnsafePath) as err:
            _LOGGER.error("%s: its mark could not be kept in its marker either: %s", managed.name, err)

    async def _contain(self, job: Job, managed: children.Managed, reason: str) -> dict:
        """An app the Supervisor installed from another definition than the manager's: the instance is marked in the
        registry first (from then on the manager starts, installs, updates or repairs nothing of it), then the app is
        stopped and uninstalled, keeping its /config folder.  Both calls go through the allow-list, which needs the
        instance's marker for them: a writer who breaks the marker blocks them, and the mark says so.  The mark,
        as recorded at the end."""
        mark = {"reason": reason[:500], "at": children.now_iso(), "uninstalled": False, "stopped": False,
                "failure": INTERRUPTED}
        if not await self._set_mark(managed.name, mark):
            job.log("the mark could not be recorded in the manager's registry: stopping it first all the same")

        boot: list = [None]  # the app's start at boot before containment, when its info could be read

        async def steps() -> None:
            try:
                info = await self.sv.app_info(managed.slug)
                boot[0] = info.get("boot")
                if info.get("state") == "started":
                    job.log("stopping it at once")
                    await self.sv.stop(managed)
                mark["stopped"] = True
                job.log("uninstalling it at once (its /config folder is kept)")
                await self.sv.uninstall(managed, remove_config=False)
                mark["uninstalled"] = True
                mark["failure"] = None
            except NotAllowed as err:
                mark["failure"] = (f"the manager's allow-list needs the instance's marker for stop and uninstall too, "
                                   f"and it refused them: {err}")
            except Exception as err:  # noqa: BLE001 - reported in the mark, the job and the log
                mark["failure"] = str(err) or err.__class__.__name__
            if not mark["uninstalled"]:
                # still installed: the Supervisor starts every app with boot auto when it starts
                await self._boot_off(job, managed, mark, boot[0], "start at boot turned off")

        task = asyncio.ensure_future(steps())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # the manager is stopping: containment goes on for CONTAIN_BOUND seconds, then its state is recorded as it
            # is (written without awaiting: this task is being cancelled), and the next start tries again
            try:
                await asyncio.wait_for(asyncio.shield(task), CONTAIN_BOUND)
            except (TimeoutError, asyncio.CancelledError):
                pass
            try:
                self.registry.update(managed.name, tampered=dict(mark), setup_complete=False)
                self._unrecorded_marks.pop(managed.name, None)
            except RegistryError as err:
                self._unrecorded_marks[managed.name] = dict(mark)
                self._trace_mark(managed, dict(mark))
                _LOGGER.error("%s", err)
            raise
        if mark["failure"]:
            job.log(f"NOT {'uninstalled' if mark['stopped'] else 'stopped'}: {mark['failure']}")
        if not await self._set_mark(managed.name, mark) and not mark["uninstalled"]:
            await asyncio.to_thread(self._trace_mark, managed, dict(mark))
        return mark

    @staticmethod
    def _mark_text(slug: str, mark: dict) -> str:
        """What a mark means for the user: done, or what to do by hand."""
        if mark.get("unverified"):
            boot = {True: "; start at boot was turned off until it is checked",
                    False: "; start at boot could NOT be turned off",
                    None: "; start at boot was off already" if "boot_manual" in mark else ""}.get(
                mark.get("boot_manual"), "")
            return (f"it was {'stopped' if mark.get('stopped') else 'NOT stopped (' + str(mark.get('failure') or INTERRUPTED)[:200] + ')'}"
                    f"{boot} and is kept installed, with its options and data. A newer manager may read the Supervisor's "
                    f"answer; until then Delete it, or start {slug} yourself in Settings > Apps if you trust it")
        if mark.get("uninstalled"):
            return "it was stopped and uninstalled at once (its /config folder is kept)"
        folder = names.folder_name(slug[len(names.SLUG_PREFIX):])
        boot = {True: "; start at boot was turned off", False: "; start at boot could NOT be turned off",
                None: "; start at boot was off already" if "boot_manual" in mark else ""}.get(
                mark.get("boot_manual"), "")
        return (f"it was NOT {'uninstalled' if mark.get('stopped') else 'stopped nor uninstalled'} "
                f"({str(mark.get('failure') or INTERRUPTED)[:300]}{boot}): stop and uninstall {slug} yourself in Settings > Apps now (keep "
                f"its data if you want it); then Delete it here, or, if the manager no longer recognises {folder}/, "
                f"delete that folder from the local apps folder and Forget it here")

    def _tampered(self, managed: children.Managed, reason: str, mark: dict, what: str) -> Tampered:
        done = self._mark_text(managed.slug, mark)
        message = (f"{reason}. {done[:1].upper()}{done[1:]}"
                   f"{' ' + what if mark.get('uninstalled') and what else ''}. Someone changed "
                   f"{names.folder_name(managed.name)}/ in the local apps folder while the manager installed it (the "
                   "addons share, SSH, another app that maps it): find out who before you create it again. The manager "
                   "starts, installs, updates or repairs nothing of it until it is deleted")
        _LOGGER.error("%s", message)
        return Tampered(message)

    async def _refuse_marked_now(self, name: str, check: bool = False) -> dict | None:
        """At the top of a job body: the registry's mark read again, as a containment may have finished since the
        request was checked (_refuse_marked).  JobFailed while it refuses the job; the entry otherwise."""
        try:
            entry = await asyncio.to_thread(self.registry.get, name)
            self._refuse_marked(name, entry, check)
        except (RegistryError, InvalidRequest) as err:
            raise JobFailed(str(err)) from None
        return entry

    def _refuse_marked(self, name: str, entry: dict | None, check: bool = False) -> None:
        """InvalidRequest while the registry marks the instance (its app was installed from another definition).
        ``check``: Finish setup or Repair, which check the installed app again: allowed for a mark of an app the manager
        could not check ("unverified"), which they clear when the check passes."""
        mark = (entry or {}).get("tampered") or self._unrecorded_marks.get(name)
        if isinstance(mark, dict) and check and mark.get("unverified"):
            return
        if isinstance(mark, dict) and mark.get("unverified"):
            raise InvalidRequest(f"{name} is marked as not checked: {str(mark.get('reason'))[:300]}. Check again (or "
                                 "Repair, without its folder) checks it; the manager starts, installs or updates "
                                 "nothing of it before")
        if isinstance(mark, dict):
            raise InvalidRequest(f"{name} is marked: the Supervisor installed another definition of it than the "
                                 "manager's. The manager starts, installs, updates or repairs nothing of it; Delete "
                                 "it (its /config folder is kept) and create it again once you know who changed its "
                                 "definition")

    async def _rollback_create(self, job: Job, managed: children.Managed, interrupted: bool = False) -> str | None:
        """Undo a create: uninstall it if the Supervisor has it, remove its definition, and forget it, or keep it as
        install interrupted when the Supervisor may still install it (``interrupted``) or cannot be asked.  None when
        that was done as it should; otherwise what is left, for the job's message."""
        try:
            installed: bool | None = any(a.get("slug") == managed.slug for a in await self.sv.list_apps())
        except (SupervisorError, NotAllowed) as err:
            installed = None
            job.log(f"whether the Supervisor installed it could not be asked ({err})")
        try:
            if installed:
                # remove_config False: a folder of an earlier instance of the same name (deleted with its data kept)
                # was reused by this install, and a failed create must not take it
                job.log("uninstalling")
                await self.sv.uninstall(managed, remove_config=False)
            job.log("removing the definition")
            await asyncio.to_thread(children.remove, managed)
            if installed is None or (interrupted and not installed):
                # the Supervisor may still be installing what it was asked to (or has, unknown): if it finishes, the
                # list shows a detached app, and the registry says why
                await asyncio.to_thread(self.registry.update, managed.name, interrupted=True, setup_complete=False,
                                        interrupted_at=children.now_iso())
            else:
                await asyncio.to_thread(self._forget, managed.name, managed.marker["instance_id"])
            await self.sv.reload_store()
        except Exception as err:  # noqa: BLE001 - the original failure is what the job reports
            job.log(f"rollback incomplete: {err}")
            _LOGGER.error("rollback of %s incomplete: %s", managed.slug, err)
            return (f"The rollback is incomplete ({err}): {names.folder_name(managed.name)} may still be in the local "
                    f"apps folder and {managed.slug} installed; the page lists what is left")
        if installed is None:
            return (f"The Supervisor could not be asked whether it installed {managed.slug}: its definition was removed, "
                    "and if the Supervisor installed it, the list shows it as install interrupted, with Repair")
        return None

    async def _installed(self, managed: children.Managed) -> dict:
        info = await self.sv.app_info(managed.slug)
        if not info.get("version"):
            raise JobFailed(f"{managed.slug} is not installed")
        return info

    async def _simple(self, job: Job, managed: children.Managed, action: str) -> dict:
        if action in ("start", "restart"):
            await self._refuse_marked_now(managed.name)
        try:
            await self._installed(managed)
            job.log(f"{action} {managed.slug}")
            await {"start": self.sv.start, "stop": self.sv.stop, "restart": self.sv.restart}[action](managed)
            info = await self.sv.app_info(managed.slug)
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        return {"state": info.get("state")}

    async def _update(self, job: Job, managed: children.Managed, version: str | None, ref: tuple[str, str] | None,
                      user: str, requested: dict | None = None) -> dict:
        await self._refuse_marked_now(managed.name)
        try:
            info = await self._installed(managed)
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        if isinstance(managed.entry.get("updating"), dict):
            # an earlier update's flag, still set (its record failed): settled first, or this update's own flag, and
            # its rollback, would lose what it names
            try:
                notes = await asyncio.to_thread(children.settle_instance, self.root, self.registry, managed.name,
                                                info.get("version"))
                managed = await asyncio.to_thread(children.load_managed, self.root, managed.name, self.registry)
                aside = await asyncio.to_thread(lambda: [e for e in os.listdir(self.root)
                                                         if e.startswith(f"{children.OLD_PREFIX}{managed.name}-")])
            except (RegistryError, children.NotManaged, children.UnsafePath, OSError) as err:
                raise JobFailed(str(err)) from None
            for note in notes:
                job.log(note)
            if aside:
                # this update would set its own previous definition aside and leave that one to be deleted
                raise JobFailed(f"an earlier update of {managed.name} left its previous definition aside ({aside[0]}) "
                                f"and it could not be settled ({'; '.join(notes)[:300]}): nothing was written")
            # an update recorded late is marked to be checked first: this one must not record over that mark
            await self._refuse_marked_now(managed.name)
        marker = managed.marker
        channel = marker["channel"]
        restamp = False
        recorded_access = self._access_of(managed.entry)  # the registry's, never the marker's
        had = dict(recorded_access)
        requested = {a: (requested or {}).get(a) for a in ACCESS}
        access = {a: had[a] if requested[a] is None else requested[a] for a in ACCESS}
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
            if info.get("version") == version:
                had = await self._access_at_same_version(managed.slug, requested, had, managed.entry)
                access = dict(had)
            if version == marker.get("version") and info.get("version") == version:
                # unchanged only when the record says what the app has: otherwise rewritten to it, and recorded
                if marker.get("stamp_version") == stamp.STAMP_VERSION and access == recorded_access:
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
            if info.get("version") == names.git_version(sha):
                had = await self._access_at_same_version(managed.slug, requested, had, managed.entry)
                access = dict(had)
            if sha == marker.get("sha") and info.get("version") == marker.get("version"):
                if marker.get("stamp_version") == stamp.STAMP_VERSION and access == recorded_access:
                    job.log(f"the {new_ref[0]} {new_ref[1]} is still {sha[:12]}: nothing to rebuild")
                    return {"version": marker.get("version"), "unchanged": True}
                restamp = True
            version = names.git_version(sha)
            if version == marker.get("version") and sha != marker.get("sha"):
                raise JobFailed(f"commit {sha[:12]} has the same version {version} as the installed {marker.get('sha', '')[:12]}: "
                                "the Supervisor would not install it")
            job.log(f"commit {sha[:12]}: version {version}")
        if access["host_network"]:
            # the version written: the same one again (a restamp) as much as a new one
            self._refuse_host_network(channel, version, archive, new_ref)
        # before the flag, the new definition and its rollback's store reload: not while a decoy is there
        await self._refuse_decoys()
        new_marker = self._marker(managed.name, channel, version, new_ref, sha, source, user, previous=marker,
                                  access=access)
        recorded = {"tag_moved": None, "tampered": None, "history": self._history(new_marker["history"]),
                    **{k: new_marker[k] for k in ("version", "ref_kind", "ref", "sha", "updated_at", "stamp_version",
                                                  "created_by", "updated_by", *ACCESS)}}
        for chosen, (key, label, what) in ACCESS.items():
            if access[chosen] != had[chosen]:
                job.log(f"{label} {'on' if access[chosen] else 'off'}: {'with' if access[chosen] else 'without'} "
                        f"{what} ({key}), from this version on")
        # before the swap: a manager killed before the update is recorded finds the flag at its next start, and puts the
        # previous definition back (children.cleanup_stale)
        try:
            await asyncio.to_thread(self.registry.update, managed.name,
                                    updating={"at": children.now_iso(), "fields": recorded})
        except RegistryError as err:
            raise JobFailed(f"nothing was written: {err}") from None
        job.log(f"rewriting {names.folder_name(managed.name)} (the previous definition is kept until the update succeeds)")
        built: dict = {}

        def record_config(digest: str) -> None:
            # in the flag before the swap: the definition in place is then the manager's, update in flight or not
            recorded["config_sha256"] = digest
            self.registry.update(managed.name, updating={"at": children.now_iso(), "fields": recorded})

        try:
            replacement = await asyncio.to_thread(
                children.replace, managed, self._builder(job, archive, channel, managed.name, version, sha, new_marker, source,
                                                         built=built, access=access, on_built=record_config))
        except WRITE_ERRORS as err:
            await self._clear_updating(managed.name)
            raise JobFailed(f"the definition was not written: {err}") from None
        expected = stamp.expected_view(built["config"], managed.slug)
        missing: list[str] = []
        unsearched: str | None = None
        sent = verified = False
        try:
            managed = await asyncio.to_thread(children.load_managed, self.root, managed.name, self.registry)
            await self._wait_store(job, managed.slug, version, replacement.manifest)
            if info.get("version") != version:
                await self._verify_store(job, managed, expected, replacement.manifest)
                job.log(f"updating {info.get('version')} -> {version}" + (" (building)" if channel == "git" else ""))
                sent = True
                await self.sv.update(managed)
                unsearched = await self._check_installed_source(managed, replacement.manifest)
                missing = await self._verify_installed(job, managed, expected)
                verified = not missing and not unsearched
            elif version != marker.get("version"):
                # the Supervisor finished an update the manager stopped waiting for: the app is taken over only as
                # checked (a restamp at the same version is not: a newer stamping may report otherwise)
                job.log(f"{managed.slug} is already at {version}: checking what the Supervisor installed")
                missing = await self._verify_installed(job, managed, expected)
            after = await self.sv.app_info(managed.slug)
            if after.get("version") != version:
                raise JobFailed(f"the Supervisor reports {after.get('version')} after the update, not {version}")
        except asyncio.CancelledError:
            if sent:
                # the Supervisor may finish the update on its own: putting the previous definition back now could
                # offer a downgrade; the next start keeps whichever definition the installed app is (cleanup_stale)
                job.log("the manager is stopping while the Supervisor updates it: both definitions stay, and the next "
                        "start keeps the new one if the Supervisor installed it, or puts the previous one back")
                raise
            job.log("the manager is stopping: putting the previous definition back")
            await self._shielded(job, self._rollback_update(job, replacement), "putting it back")
            raise
        except Tampered as err:
            job.log(f"{err}: stopping and uninstalling it, and putting the previous definition back")
            mark = await self._contain(job, managed, str(err))
            await self._rollback_update(job, replacement)
            raise self._tampered(managed, str(err), mark, "and its previous definition put back") from None
        except Exception as err:
            if sent:
                await self._after_failed_update(job, managed, replacement, version, recorded, err, verified,
                                                built["files"], access)
            job.log(f"{err}: putting the previous definition back")
            await self._rollback_update(job, replacement)
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        # the record first, then the previous definition goes: stopped between the two, the next start finds no flag
        # and removes the previous definition (children.cleanup_stale), so the update stands either way
        warnings = []
        try:
            await asyncio.to_thread(self.registry.update, managed.name, updating=None, **recorded)
        except RegistryError as err:
            warnings.append(f"the manager's registry was not updated ({err}); its flag lets the next start record "
                            "the update")
        try:
            await asyncio.to_thread(replacement.commit)
        except OSError as err:
            warnings.append(f"the previous definition was not removed ({err}); "
                            + ("the next start removes it" if not warnings else
                               "with the record missing too, the next start puts it back: Update again then"))
        warning = f"updated to {version}, but " + "; and ".join(warnings) if warnings else None
        if warning:
            job.log(f"warning: {warning}")
            _LOGGER.warning("%s: %s", managed.name, warning)
        await self._save_copy(job, managed, access, built["files"])
        if missing or unsearched:  # updated and recorded, as far as the manager can tell: stopped and marked, kept
            raise await self._hold(job, managed, missing, reason=unsearched)
        if restamp:
            job.log(f"the definition is stamped by this manager now (stamping {marker.get('stamp_version')} -> "
                    f"{stamp.STAMP_VERSION}); the manager applies it to the running app at its next version change (an "
                    "HRI update, or a rebuild of a new commit). The Supervisor's own Rebuild would apply it at once, "
                    "but without the manager's checks: do not use it")
        result = {"version": version, "state": after.get("state"), "restamped": restamp}
        return {**result, "warning": warning} if warning else result

    async def _after_failed_update(self, job: Job, managed: children.Managed, replacement: children.Replacement,
                                   version: str, recorded: dict, err: Exception, verified: bool,
                                   files: dict[str, bytes], access: dict) -> None:
        """An update call that failed after it was sent: what the Supervisor has now decides.  Installed all the same:
        the new definition stays and is recorded (marked to be checked unless it was), never the previous one put
        back (the Supervisor would offer a downgrade).  Not known yet (no answer, or no clean refusal): both stay for
        the next start to settle.  Returns only when the update did not happen: the caller puts the previous one back."""
        try:
            now = (await self.sv.app_info(managed.slug)).get("version")
        except Exception:  # noqa: BLE001 - not known
            now = None
        unsure = isinstance(err, SupervisorError) and not (err.status and 400 <= err.status < 500)
        if now == version:
            mark = None if verified else children.pending_mark(
                f"its update to {version} was not checked: the update call failed ({str(err)[:200]}) after the "
                "Supervisor had installed it")
            try:
                await asyncio.to_thread(self.registry.update, managed.name, updating=None, **{**recorded, "tampered": mark})
                await asyncio.to_thread(replacement.commit)
            except (RegistryError, OSError) as err2:
                job.log(f"warning: {err2}; the next start records it")
            await self._save_copy(job, managed, access, files)
            raise JobFailed(f"{err}. The Supervisor has installed {version} all the same: its new definition stays and "
                            "is recorded" + ("" if verified else "; the manager has not checked it yet: Check again "
                                                               "on its row")) from None
        if now is None or unsure:
            raise JobFailed(f"{err}. Whether the Supervisor installed {version} is not known yet: both definitions "
                            "stay, and the manager's next start keeps the new one if it did, or puts the previous one "
                            "back") from None

    async def _installed_access(self, slug: str, recorded: dict[str, bool]) -> dict[str, bool]:
        """Each access (ACCESS) the installed app has, as the Supervisor reports it (its host_dbus, host_network; the
        registry's ``recorded`` when it does not): what a definition of the installed version must say, the registry's
        record lagging or not."""
        try:
            definition = await self.sv.app_definition(slug)
        except (SupervisorError, NotAllowed) as err:
            raise JobFailed(str(err)) from None
        return {access: definition[key] if isinstance(definition.get(key), bool) else recorded[access]
                for access, (key, _, _) in ACCESS.items()}

    @staticmethod
    def _access_confirmed(entry: dict | None, access: str, installed: bool, requested: bool | None) -> bool:
        """Whether ``installed`` (what the app has of ``access``) may be recorded as the instance's choice where the
        registry says the other: the admin chose it (``requested``), or the manager made that change itself, in an
        update whose record is late (the registry's ``updating`` flag names it)."""
        if requested is not None:
            return requested is installed
        flag = (entry or {}).get("updating")
        fields = flag.get("fields") if isinstance(flag, dict) and isinstance(flag.get("fields"), dict) else {}
        return fields.get(access) is installed

    @staticmethod
    def _access_not_chosen(slug: str, access: str, installed: bool, detached: bool = False) -> str:
        _, label, what = ACCESS[access]
        return NOT_CHOSEN.format(
            slug=slug, has="has" if installed else "does not have", what=what, label=label,
            record="off" if installed else "on",
            choose=CHOOSE_DETACHED.format(label=label) if detached
            else CHOOSE.format(label=label, state="on" if installed else "off"))

    async def _access_at_same_version(self, slug: str, requested: dict[str, bool | None], recorded: dict[str, bool],
                                      entry: dict | None) -> dict[str, bool]:
        """Each access to write when the installed app is already at the version being written (a catch-up after an
        update the manager stopped waiting for, or a restamp): the Supervisor applies nothing then, so the definition
        takes what the app has (its host_dbus and host_network, as it reports them), and a request for the other is
        refused (NEEDS_VERSION).  What the app has, when the registry says otherwise, only as the admin's choice or
        the manager's own change (_access_confirmed)."""
        installed = await self._installed_access(slug, recorded)
        for access, (_, label, what) in ACCESS.items():
            want = requested.get(access)
            if want is not None and want != installed[access]:
                raise JobFailed(NEEDS_VERSION.format(label=label, what=what, state="on" if want else "off"))
            if installed[access] != recorded[access] and not self._access_confirmed(entry, access, installed[access], want):
                raise JobFailed(self._access_not_chosen(slug, access, installed[access]))
        return installed

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
            # what the Supervisor would build or confine the app with instead of what the manager writes (the
            # manager writes none of them; build_git refuses a tree with one)
            build_files = sorted(rel for rel in manifest if "/" not in rel and stamp.BUILD_FILES_RE.fullmatch(rel))
            if build_files:
                raise copies.CopyError(f"it holds {', '.join(tarsafe.show(r) for r in build_files)}, which the Supervisor "
                                       "would use to build or confine the app")
            raw = copies.read_file(os.path.join(folder, "config.yaml"))
            if manifest.get("config.yaml", "").split(" ")[0] != "sha256:" + hashlib.sha256(raw).hexdigest():
                raise copies.CopyError("its config.yaml changed while it was read")
            config = copies.check(yaml.safe_load(raw.decode("utf-8")), managed.name, str(managed.marker.get("version")),
                                  managed.entry.get("channel"), **self._access_of(managed.entry))
        except (copies.CopyError, OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as err:
            raise JobFailed(f"{names.folder_name(managed.name)} is not a definition this manager writes ({str(err)[:300]}): not "
                            "installed; Delete removes it") from None
        return stamp.expected_view(config, managed.slug), manifest

    async def _refuse_host_network_installed(self, job: Job, managed: children.Managed) -> None:
        """_refuse_host_network for the definition of the installed version, which is stamped again in place: the
        archive of its release (the commit the registry recorded), or of its commit, downloaded to be checked."""
        channel, version, sha = managed.entry.get("channel"), str(managed.marker.get("version")), managed.marker.get("sha")
        if channel == "release":
            ref = ("tag", f"v{version}")
            archive, _ = await self._fetch(job, channel, version, ref, recorded=managed.entry.get("sha"))
        else:
            try:
                ref = names.validate_ref(managed.marker.get("ref_kind"), managed.marker.get("ref"))
            except ValueError as err:
                raise JobFailed(f"the manager has no usable branch or tag for {managed.name}: {err}") from None
            if not isinstance(sha, str) or not names.SHA_RE.fullmatch(sha):
                raise JobFailed(f"the manager has no record of the commit of {managed.slug}")
            job.log(f"downloading commit {sha[:12]}, the installed one, to check it")
            try:
                archive, _ = await self.gh.tarball_of_commit(sha, *ref)
            except (GitHubError, NotHRICommit) as err:
                raise JobFailed(str(err)) from None
            except Exception as err:  # noqa: BLE001 - tarsafe.UnsafeArchive
                raise JobFailed(f"the archive of commit {sha[:12]} was refused: {err}") from None
            job.keep(archive)
        self._refuse_host_network(channel, version, archive, ref)

    async def _follow_access(self, job: Job, managed: children.Managed, installed: dict[str, bool]) -> children.Managed:
        """The installed app has ``installed`` (host_dbus, host_network), the registry's record and the definition
        something else: the definition's config.yaml is stamped again with it (checked first as this manager writes
        it, then replaced at once), and the marker, the registry and the copy in /data record it.  The Managed read
        again.  Never to Host network off (FOLLOW_HOST_NETWORK_OFF): HRI's own ingress_port is not in the stamped
        definition; to Host network on only for an HRI that reads its port (_refuse_host_network_installed)."""
        recorded = self._access_of(managed.entry)
        if recorded["host_network"] and not installed["host_network"]:
            raise JobFailed(FOLLOW_HOST_NETWORK_OFF.format(slug=managed.slug, choose=CHOOSE.format(
                label=ACCESS["host_network"][1], state="off")))
        if installed["host_network"] and not recorded["host_network"]:
            await self._refuse_host_network_installed(job, managed)
        for access, (_, label, what) in ACCESS.items():
            if installed[access] != recorded[access]:
                job.log(f"{label}: the installed app {'has' if installed[access] else 'does not have'} {what}, the "
                        "manager's record said otherwise: the definition follows the app, and the record too")
        folder = children.child_path(self.root, managed.name)
        channel, version = managed.entry.get("channel"), str(managed.marker.get("version"))

        def rewrite() -> dict[str, bytes]:
            raw = children.read_file(folder, "config.yaml")
            config = copies.check(yaml.safe_load(raw.decode("utf-8")), managed.name, version, channel, **recorded)
            # the keys stamping adds for an access; ingress_port stays (stamping writes 0 again for Host network)
            template = {k: v for k, v in config.items() if k not in ("host_dbus", "host_network")}
            new = stamp.stamp({**template, "slug": names.HRI_SLUG}, managed.name, version, channel, **installed)
            data = stamp.dump(new, managed.marker["template_source"])
            previous = {k: managed.entry.get(k) for k in (*ACCESS, "config_sha256")}
            # the record first: when the registry cannot be written, the definition stays as it was (a definition and
            # a record that disagree refuse every later check of it)
            self.registry.update(managed.name, **installed, config_sha256=hashlib.sha256(data).hexdigest())
            try:
                children.replace_file(folder, "config.yaml", data)
                children.replace_file(folder, children.MARKER, children.marker_bytes({**managed.marker, **installed}))
            except BaseException:
                try:  # both back as they were
                    children.replace_file(folder, "config.yaml", raw)
                    children.replace_file(folder, children.MARKER, children.marker_bytes(managed.marker))
                    self.registry.update(managed.name, **previous)
                except (OSError, children.UnsafePath, RegistryError) as err:
                    _LOGGER.error("%s: its definition and its record of its access to the host may disagree now: %s",
                                  managed.name, err)
                raise
            try:
                kept = copies.read_definition(copies.folder(self.copies_root, managed.name), channel)
            except (OSError, children.UnsafePath):
                kept = {}
            return {**kept, "config.yaml": data}

        try:
            files = await asyncio.to_thread(rewrite)
            managed = await asyncio.to_thread(children.load_managed, self.root, managed.name, self.registry)
        except (copies.CopyError, children.UnsafePath, children.NotManaged, stamp.TemplateError, RegistryError, OSError,
                UnicodeDecodeError, yaml.YAMLError, RecursionError) as err:
            raise JobFailed(f"{names.folder_name(managed.name)} could not follow the installed app's access to the host: "
                            f"{err}") from None
        await self._save_copy(job, managed, installed, files)
        return managed

    async def _setup(self, job: Job, managed: children.Managed, install: bool) -> dict:
        entry = await self._refuse_marked_now(managed.name, check=not install)
        mark = (entry or {}).get("tampered")
        # Check again of a hold that found start at boot off (the admin's choice) leaves it off
        boot = not (isinstance(mark, dict) and "boot_manual" in mark and mark["boot_manual"] is None)
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
                unsearched = await self._check_installed_source(managed, manifest)
                missing = await self._verify_installed(job, managed, expected)
                if missing or unsearched:
                    raise await self._hold(job, managed, missing, reason=unsearched)
            elif not installed:
                raise JobFailed(f"{managed.slug} is not installed: Install it")
            else:
                # installed by a create the manager did not finish: taken over only once checked against its folder,
                # and never started while a decoy the store may take is there
                await self._refuse_decoys()
                recorded = self._access_of(managed.entry)
                installed_access = await self._installed_access(managed.slug, recorded)
                if installed_access != recorded:
                    for access in ACCESS:
                        if (installed_access[access] != recorded[access]
                                and not self._access_confirmed(managed.entry, access, installed_access[access], None)):
                            raise JobFailed(self._access_not_chosen(managed.slug, access, installed_access[access]))
                    managed = await self._follow_access(job, managed, installed_access)
                expected, manifest = await asyncio.to_thread(self._definition_on_disk, managed)
                await self._check_tree(managed.slug, manifest)
                missing = await self._verify_installed(job, managed, expected)
                if missing:
                    raise await self._hold(job, managed, missing)
            await self._finish_setup(job, managed, boot=boot)
        except Tampered as err:
            mark = await self._contain(job, managed, str(err))
            raise self._tampered(managed, str(err), mark, "") from None
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

    async def _detached(self, job: Job, name: str, action: str = "repair") -> tuple[dict, dict]:
        """The registry's entry and the Supervisor's info of an instance whose definition is gone: in the registry,
        no folder, installed, detached after a store reload, no store entry, HRI's url.  JobFailed otherwise, in the
        words of ``action`` (repair, update, delete)."""
        slug = names.supervisor_slug(name)
        done = {"repair": "repaired", "update": "updated", "delete": "deleted"}.get(action, action)
        try:
            entry = await asyncio.to_thread(self.registry.get, name)
        except RegistryError as err:
            raise JobFailed(str(err)) from None
        if entry is None:
            raise JobFailed(f"{name} is not in the manager's registry: not created by this manager, left alone")
        if os.path.lexists(os.path.join(self.root, names.folder_name(name))):
            raise JobFailed(f"{names.folder_name(name)} exists: {name} is not an instance without its definition, so "
                            f"it is not {done} this way")
        try:
            if not any(a.get("slug") == slug for a in await self.sv.list_apps()):
                raise JobFailed(f"{slug} is not installed: nothing to {action}"
                                + (" (Forget drops the manager's own records of it)" if action == "delete" else ""))
            # only an app whose definition is gone from everywhere the store reads: after a reload, the Supervisor
            # calls it detached and its store has no app of that slug
            await self.sv.reload_store()
            info = await self.sv.app_info(slug)
            if info.get("detached") is not True:
                raise JobFailed(f"{slug} is not detached: a definition of it is in the local apps folder (in another "
                                f"folder than {names.folder_name(name)}?), so it is not the manager's to {action}: left alone")
            if await self.sv.store_app(slug) is not None:
                raise JobFailed(f"the Supervisor's store has a definition of {slug}: not {done}, left alone")
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
        await self._refuse_marked_now(name, check=True)
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
        # a Repair writes a definition and reloads the store, as an install does: not while a decoy is there
        await self._refuse_decoys()
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
        # the definition of the installed version says what the installed app has: its host_dbus and host_network,
        # when the registry's record lags behind a change the manager made (an update recorded late); any other
        # difference is not recorded without the admin (an older /data restored, or the Supervisor's own Update of a
        # changed folder).  Written from HRI's template (or a copy stamped alike), so Host network off gets HRI's port
        recorded_access = self._access_of(entry)
        installed_access = await self._installed_access(slug, recorded_access)
        for access, (_, label, what) in ACCESS.items():
            if installed_access[access] == recorded_access[access]:
                continue
            if not self._access_confirmed(entry, access, installed_access[access], None):
                raise NeedsAttention(self._access_not_chosen(slug, access, installed_access[access], detached=True))
            job.log(f"{label}: the installed app {'has' if installed_access[access] else 'does not have'} {what}, the "
                    "manager's record said otherwise: the definition follows the app, and the record too")
        entry = {**entry, **installed_access}
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
                job.keep(archive)
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
        access = self._access_of(entry)
        marker = self._marker(name, channel, version, ref, sha, source, user, instance_id=entry.get("instance_id"),
                              access=access)
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
        built: dict = {}
        try:
            # a mark stays until the check below passes
            await asyncio.to_thread(self.registry.put, name, self._registry_entry(marker, setup_complete=setup_complete,
                                                                                  tampered=entry.get("tampered")))
            managed = await asyncio.to_thread(
                children.write_new, self.root, name,
                self._builder(job, archive, channel, name, version, sha, marker, source, copy=copy, built=built,
                              access=access, on_built=self._record_config(name)),
                self.registry)
        except WRITE_ERRORS as err:
            await self._undo_registry(job, self._restore_entry, name, entry, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        self._clear_auto(name, keep_job=job.id)  # an automatic repair's own note stays: it succeeded
        await self._save_copy(job, managed, access, built["files"])
        await self._wait_store(job, slug, version, managed.manifest)
        # the app keeps running as the Supervisor installed it: taken over only once it reports the definition written
        try:
            missing = await self._verify_installed(job, managed, stamp.expected_view(built["config"], slug))
        except Tampered as err:
            mark = await self._contain(job, managed, str(err))
            raise self._tampered(managed, str(err), mark, "") from None
        if missing:
            raise await self._hold(job, managed, missing)
        mark = entry.get("tampered") or self._unrecorded_marks.get(name)
        if mark is not None:
            await asyncio.to_thread(self.registry.update, name, tampered=None)
            self._unrecorded_marks.pop(name, None)
            job.log("checked: the mark goes")
            await self._boot_back(job, managed, mark)
        return {"slug": slug, "version": version}

    async def _update_detached(self, job: Job, name: str, version: str | None, ref: tuple[str, str] | None,
                               user: str, requested: dict | None = None) -> dict:
        """Update (release) or Rebuild (git) of an instance whose definition is gone and could not be written again at
        its installed version: the definition of a NEWER version, written and installed in one job the user asked
        for.  If the update does not succeed the definition is removed again (the instance stays detached)."""
        slug = names.supervisor_slug(name)
        await self._refuse_marked_now(name)
        # it writes a definition (and removes it again on failure, with a store reload): not while a decoy is there
        await self._refuse_decoys()
        entry, info = await self._detached(job, name, "update")
        installed = str(info.get("version") or "")
        channel = entry.get("channel")
        # the registry's choices: the default for a newer version (never what the installed app has, which the manager
        # may not have made); at the installed version only the app's, as the admin's choice (_access_at_same_version)
        recorded = self._access_of(entry)
        requested = {a: (requested or {}).get(a) for a in ACCESS}
        access = {a: recorded[a] if requested[a] is None else requested[a] for a in ACCESS}
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
                access = await self._access_at_same_version(slug, requested, recorded, entry)
                return await self._adopt_installed_commit(job, name, {**entry, **access}, info, new_ref,
                                                          archive, source, user)
        if access["host_network"]:
            self._refuse_host_network(channel, version, archive, new_ref)
        sha = archive.sha
        # the registry's entry as the previous marker (the folder is gone): who created it, its history, this update
        marker = self._marker(name, channel, version, new_ref, sha, source, user,
                              previous={**entry, "history": self._history(entry.get("history"))},
                              instance_id=entry.get("instance_id"), access=access)
        marker["created_at"] = entry.get("created_at") or marker["created_at"]
        job.log(f"writing {names.folder_name(name)} for {version} (installed: {installed}), then updating")
        built: dict = {}
        try:
            await asyncio.to_thread(self.registry.put, name,
                                    self._registry_entry(marker, setup_complete=entry.get("setup_complete", True)))
            managed = await asyncio.to_thread(
                children.write_new, self.root, name,
                self._builder(job, archive, channel, name, version, sha, marker, source, built=built, access=access,
                              on_built=self._record_config(name)),
                self.registry)
        except WRITE_ERRORS as err:
            await self._undo_registry(job, self._restore_entry, name, entry, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        expected = stamp.expected_view(built["config"], slug)
        try:
            await self._wait_store(job, slug, version, managed.manifest)
            await self._verify_store(job, managed, expected, managed.manifest)
            job.log(f"updating {installed} -> {version}" + (" (building)" if channel == "git" else ""))
            await self.sv.update(managed)
            unsearched = await self._check_installed_source(managed, managed.manifest)
            missing = await self._verify_installed(job, managed, expected)
            after = await self.sv.app_info(slug)
            if after.get("version") != version:
                raise JobFailed(f"the Supervisor reports {after.get('version')} after the update, not {version}")
        except asyncio.CancelledError:
            job.log("the manager is stopping: removing the definition again")
            await self._shielded(job, self._undo_detached(job, managed, entry), "removing it")
            raise
        except Tampered as err:
            job.log(f"{err}: stopping and uninstalling it, and removing the definition again")
            mark = await self._contain(job, managed, str(err))
            await self._undo_detached(job, managed, entry)
            await self._set_mark(name, mark)  # the entry put back has no mark
            raise self._tampered(managed, str(err), mark, "and its definition removed again") from None
        except Exception as err:
            job.log(f"{err}: removing the definition again (the instance stays as it was)")
            await self._undo_detached(job, managed, entry)
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        self._clear_auto(name)
        await self._save_copy(job, managed, access, built["files"])
        if missing or unsearched:
            raise await self._hold(job, managed, missing, reason=unsearched)
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
        entry, info = await self._detached(job, name, "delete")
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
