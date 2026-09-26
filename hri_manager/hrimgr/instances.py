"""What the manager does: list, create, update, start/stop/restart, delete and repair HRI instances.

Every action that changes something runs as a job (jobs.py).  An action on an instance first loads its
``children.Managed`` (the marker check); the Supervisor client refuses any changing call without it.  A create
that fails is rolled back (uninstalled if it got that far, its folder removed, the store reloaded); an update that
fails puts the previous definition back."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

from . import VERSION, children, names, stamp
from .github import GitHub, GitHubError, latest_stable
from .jobs import Job, JobFailed, Jobs
from .registry import Registry, RegistryError
from .supervisor import NotAllowed, SupervisorClient, SupervisorError

_LOGGER = logging.getLogger(__name__)
HISTORY = 20


class InvalidRequest(ValueError):
    pass


REGISTRY_FIELDS = ("name", "slug", "channel", "version", "ref_kind", "ref", "sha", "instance_id", "created_at", "updated_at")


class Manager:
    def __init__(self, local_apps: str, supervisor: SupervisorClient, github: GitHub, jobs: Jobs, registry: Registry, *,
                 dev: bool = False, poll_interval: float = 2.0, store_timeout: float = 90.0):
        self.root = local_apps
        self.sv = supervisor
        self.gh = github
        self.jobs = jobs
        self.registry = registry
        self.dev = dev
        self.poll_interval = poll_interval
        self.store_timeout = store_timeout

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
        out, others, seen = [], [], set()
        for name, marker, problem in folders:
            slug = names.supervisor_slug(name)
            seen.add(slug)
            if marker is None:
                others.append({"slug": slug, "name": names.folder_name(name), "kind": "folder", "problem": problem,
                               "installed": slug in installed, "state": installed.get(slug, {}).get("state")})
                continue
            out.append(self._entry(name, marker, installed.get(slug), latest))
        for slug, app in sorted(installed.items()):
            name = names.name_from_slug(slug)
            if name and slug not in seen:
                entry = self._entry(name, None, app, latest)
                # detached: the Supervisor has no definition of it anywhere (a hand-made local app with this slug is
                # not detached, and Repair would write a second definition of its slug)
                if app.get("url") == names.HRI_URL and app.get("detached") is True:
                    entry["problem"] = "installed, but its definition folder is gone (a partial restore?): repair writes it again"
                    entry["actions"] = ["repair"]
                    out.append(entry)
                else:
                    others.append({"slug": slug, "name": app.get("name"), "kind": "local", "installed": True,
                                   "state": app.get("state"), "version": app.get("version"),
                                   "problem": "a local app the manager did not create"})
            elif slug.endswith("_" + names.HRI_SLUG):
                others.append({"slug": slug, "name": app.get("name"), "kind": "published", "installed": True,
                               "state": app.get("state"), "version": app.get("version"),
                               "update_available": bool(app.get("update_available"))})
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

    async def _info(self, slug: str) -> dict | None:
        try:
            return await self.sv.app_info(slug)
        except (SupervisorError, NotAllowed) as err:
            _LOGGER.warning("info of %s: %s", slug, err)
            return None

    def _entry(self, name: str, marker: dict | None, app: dict | None, latest: dict | None) -> dict:
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
            "job": job.summary() if job else None, "actions": [],
        }
        if marker is None:
            return entry
        if (marker.get("channel") == "release" and latest
                and (names.parse_version(latest["version"]) or ()) > (names.parse_version(marker.get("version")) or ())):
            entry["newer_release"] = latest["version"]
        actions = []
        if app is None:
            entry["problem"] = "defined, but not installed"
        else:
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

    def update(self, name: str, body: dict, user: str) -> Job:
        managed = self.managed(name)
        version, ref = body.get("version"), None
        if managed.marker["channel"] == "release":
            if version is not None and (not isinstance(version, str) or not names.parse_version(version)):
                raise InvalidRequest("Choose an HRI release.")
        else:
            version = None
            if body.get("ref") is not None:
                ref = self.check_ref(body.get("ref_kind"), body.get("ref"))
        return self.jobs.start(name, "update", user, lambda job: self._update(job, managed, version, ref, user))

    def delete(self, name: str, body: dict, user: str) -> Job:
        managed = self.managed(name)
        remove_data = body.get("remove_data", False)
        if not isinstance(remove_data, bool):
            raise InvalidRequest("remove_data is true or false.")
        if remove_data and body.get("confirm") != name:
            raise InvalidRequest(f"Deleting the data too needs the instance's name typed: {name}.")
        return self.jobs.start(name, "delete", user, lambda job: self._delete(job, managed, remove_data))

    def repair(self, name: str, user: str) -> Job:
        name = self.check_name(name)
        return self.jobs.start(name, "repair", user, lambda job: self._repair(job, name, user))

    # ------------------------------------------------------------------ job bodies

    async def _wait_store(self, job: Job, slug: str, version: str) -> None:
        job.log("reloading the Supervisor's store")
        await self.sv.reload_store()
        waited = 0.0
        reloaded_again = False
        while True:
            entry = await self.sv.store_app(slug)
            if entry and entry.get("version") == version:
                job.log(f"the store has {slug} {version}")
                return
            if waited >= self.store_timeout:
                raise JobFailed(f"the Supervisor's store did not show {slug} {version} within {int(self.store_timeout)} s")
            if not reloaded_again and waited >= self.store_timeout / 2:
                await self.sv.reload_store()
                reloaded_again = True
            await asyncio.sleep(self.poll_interval)
            waited += self.poll_interval

    async def _fetch(self, job: Job, channel: str, version: str | None, ref: tuple[str, str] | None):
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
        return archive, url

    async def _check_release(self, version: str) -> None:
        try:
            releases = await self.gh.releases()
        except GitHubError as err:
            raise JobFailed(f"the release list: {err}") from None
        if not any(r["version"] == version for r in releases):
            raise JobFailed(f"hass-remote-integration {version} is not a published release (0.25.0 or newer)")

    def _marker(self, name: str, channel: str, version: str, ref: tuple[str, str], sha: str | None, source: str, user: str,
                previous: dict | None = None, instance_id: str | None = None) -> dict:
        now = children.now_iso()
        marker = {
            "manager": children.MANAGER_ID, "manager_version": VERSION, "name": name, "slug": names.supervisor_slug(name),
            "channel": channel, "version": version, "ref_kind": ref[0], "ref": ref[1], "sha": sha,
            "instance_id": (previous or {}).get("instance_id") or instance_id or secrets.token_hex(16),
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
        return {**{k: marker.get(k) for k in REGISTRY_FIELDS}, "setup_complete": False, **extra}

    def _forget(self, name: str, instance_id: str) -> None:
        """Drop the registry's entry of ``name``, when it is still the one of that instance."""
        entry = self.registry.get(name)
        if entry and entry.get("instance_id") == instance_id:
            self.registry.remove(name)

    def _builder(self, job: Job, archive, channel: str, name: str, version: str, sha: str | None, marker: dict, source: str):
        def build(tmp: str) -> dict:
            if channel == "release":
                stamp.build_release(archive, tmp, name, version, source)
            else:
                _, notes = stamp.build_git(archive, tmp, name, version, sha, source)
                for note in notes:
                    job.log(note)
            found = stamp.find_configs(tmp)
            if found != ["config.yaml"]:
                raise JobFailed(f"the definition would hold more than one app: {found}")
            return marker
        return build

    async def _create(self, job: Job, name: str, channel: str, version: str | None, ref: tuple[str, str] | None,
                      user: str) -> dict:
        slug = names.supervisor_slug(name)
        job.log(f"checking that {slug} is free")
        if any(a.get("slug") == slug for a in await self.sv.list_apps()):
            raise JobFailed(f"an app {slug} is already installed")
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
        try:
            await asyncio.to_thread(self.registry.put, name, self._registry_entry(marker))
            managed = await asyncio.to_thread(children.write_new, self.root, name,
                                              self._builder(job, archive, channel, name, version, sha, marker, source),
                                              self.registry)
        except (stamp.TemplateError, children.UnsafePath, children.NotManaged, RegistryError, OSError) as err:
            await asyncio.to_thread(self._forget, name, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        try:
            await self._wait_store(job, slug, version)
            job.log("installing (a git build takes several minutes)" if channel == "git" else "installing (pulling the image)")
            await self.sv.install(managed)
            job.log("turning on start at boot, the Watchdog and the sidebar panel")
            await self.sv.set_options(managed, boot="auto", watchdog=True, ingress_panel=True)
            job.log("starting")
            await self.sv.start(managed)
            info = await self.sv.app_info(slug)
        except Exception as err:
            job.log(f"{err}: rolling back")
            await self._rollback_create(job, managed)
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        return {"slug": slug, "version": version, "ingress_url": info.get("ingress_url"), "state": info.get("state")}

    async def _rollback_create(self, job: Job, managed: children.Managed) -> None:
        try:
            if any(a.get("slug") == managed.slug for a in await self.sv.list_apps()):
                # remove_config False: a folder of an earlier instance of the same name (deleted with its data kept)
                # was reused by this install, and a failed create must not take it
                job.log("uninstalling")
                await self.sv.uninstall(managed, remove_config=False)
            job.log("removing the definition")
            await asyncio.to_thread(children.remove, managed)
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
            if (names.parse_version(version) or ()) < (names.parse_version(marker.get("version")) or ()):
                raise JobFailed(f"{version} is older than {marker.get('version')}: the manager does not downgrade")
            if version == marker.get("version") and info.get("version") == version:
                job.log(f"already at {version}")
                return {"version": version, "unchanged": True}
            new_ref = ("tag", f"v{version}")
        else:
            new_ref = ref or self.check_ref(marker.get("ref_kind"), marker.get("ref"))
        archive, source = await self._fetch(job, channel, version, new_ref)
        sha = archive.sha
        if channel == "git":
            if sha == marker.get("sha") and info.get("version") == marker.get("version"):
                job.log(f"the {new_ref[0]} {new_ref[1]} is still {sha[:12]}: nothing to rebuild")
                return {"version": marker.get("version"), "unchanged": True}
            version = names.git_version(sha)
            job.log(f"commit {sha[:12]}: version {version}")
        new_marker = self._marker(managed.name, channel, version, new_ref, sha, source, user, previous=marker)
        job.log(f"rewriting {names.folder_name(managed.name)} (the previous definition is kept until the update succeeds)")
        try:
            replacement = await asyncio.to_thread(
                children.replace, managed, self._builder(job, archive, channel, managed.name, version, sha, new_marker, source))
        except (stamp.TemplateError, children.UnsafePath, children.NotManaged, OSError) as err:
            raise JobFailed(f"the definition was not written: {err}") from None
        try:
            managed = children.load_managed(self.root, managed.name, self.registry)
            await self._wait_store(job, managed.slug, version)
            if info.get("version") != version:
                job.log(f"updating {info.get('version')} -> {version}" + (" (building)" if channel == "git" else ""))
                await self.sv.update(managed)
            after = await self.sv.app_info(managed.slug)
            if after.get("version") != version:
                raise JobFailed(f"the Supervisor reports {after.get('version')} after the update, not {version}")
        except Exception as err:
            job.log(f"{err}: putting the previous definition back")
            try:
                await asyncio.to_thread(replacement.rollback)
                await self.sv.reload_store()
            except Exception as err2:  # noqa: BLE001
                job.log(f"could not put it back: {err2}")
            if isinstance(err, (SupervisorError, NotAllowed)):
                raise JobFailed(str(err)) from None
            raise
        await asyncio.to_thread(replacement.commit)
        await asyncio.to_thread(self.registry.update, managed.name, **{k: new_marker[k] for k in ("version", "ref_kind", "ref", "sha", "updated_at")})
        return {"version": version, "state": after.get("state")}

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

    async def _repair(self, job: Job, name: str, user: str) -> dict:
        """Write the definition of an installed instance again: from the manager's registry when it has the
        instance (its channel, branch or tag), else from the installed version (a release only)."""
        slug = names.supervisor_slug(name)
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
        try:
            entry = await asyncio.to_thread(self.registry.get, name)
        except RegistryError as err:
            raise JobFailed(str(err)) from None
        version = str(info.get("version") or "")
        if entry and entry.get("channel") == "git":
            try:
                channel, ref = "git", names.validate_ref(entry.get("ref_kind"), entry.get("ref"))
            except ValueError as err:
                raise JobFailed(f"the manager's registry has no usable branch or tag for {name}: {err}") from None
        elif names.parse_version(version) and names.supported_version(version):
            channel, ref = "release", ("tag", f"v{version}")
            await self._check_release(version)
        elif names.GIT_VERSION_RE.fullmatch(version):
            raise JobFailed(f"{slug} is a git build ({version}) the manager's registry does not know: it cannot tell "
                            "which branch or tag to write again")
        else:
            raise JobFailed(f"cannot tell which HRI {version!r} is")
        archive, source = await self._fetch(job, channel, version if channel == "release" else None, ref)
        sha = archive.sha
        if channel == "git" and names.git_version(sha) != version:
            job.log(f"the {ref[0]} {ref[1]} is now at {sha[:12]}, not the installed {version}: the definition is written "
                    "for the new commit, and Rebuild installs it")
            version = names.git_version(sha)
        marker = self._marker(name, channel, version, ref, sha, source, user,
                              instance_id=entry.get("instance_id") if entry else None)
        marker["repaired_at"] = marker["updated_at"]
        if entry:
            marker["created_at"] = entry.get("created_at") or marker["created_at"]
        setup_complete = entry.get("setup_complete", True) if entry else True
        job.log(f"writing {names.folder_name(name)} again for {version}"
                + (" (from the manager's registry)" if entry else ""))
        try:
            await asyncio.to_thread(self.registry.put, name, self._registry_entry(marker, setup_complete=setup_complete))
            await asyncio.to_thread(children.write_new, self.root, name,
                                    self._builder(job, archive, channel, name, version, sha, marker, source), self.registry)
        except (stamp.TemplateError, children.UnsafePath, children.NotManaged, RegistryError, OSError) as err:
            await asyncio.to_thread(self._restore_entry, name, entry, marker["instance_id"])
            raise JobFailed(f"the definition was not written: {err}") from None
        await self._wait_store(job, slug, version)
        return {"slug": slug, "version": version}

    def _restore_entry(self, name: str, entry: dict | None, instance_id: str) -> None:
        if entry is not None:
            self.registry.put(name, entry)
        else:
            self._forget(name, instance_id)
