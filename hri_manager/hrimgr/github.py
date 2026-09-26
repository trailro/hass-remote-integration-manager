"""HRI's releases and source tarballs, from GitHub.

Two fixed hosts, no redirects followed: api.github.com for the release list and refs (with the optional token, for the
rate limit) and codeload.github.com for tarballs (never with the token: HRI is public).  A git ref is a branch or a
tag of HRI itself (names.validate_ref), checked to exist in HRI's repository with the API before anything is
downloaded, and fetched with its full ``refs/heads/`` or ``refs/tags/`` path: codeload also serves pull requests and
forks' commits under HRI's name, which a short name could reach.  One exception, for Repair only: the commit a git
instance was built from, which the manager recorded itself (from HRI's branch or tag, in its own /data) and never
takes from a request.  It is downloaded only after GitHub's compare says it is on HRI's own branch or tag (codeload
serves a fork's commits under HRI's name too), and the archive must name that very commit.  The release list is cached in /data for an hour,
and the instances list reads only the cache, so GitHub being unreachable never stops the page."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from . import names, tarsafe
from .httpread import TooLarge, read_capped

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api.github.com"
CODELOAD_URL = "https://codeload.github.com"
CACHE_TTL = 3600
MIN_REFRESH = 60
MAX_RELEASES_JSON = 4 * 1024 * 1024


class GitHubError(Exception):
    pass


class NotHRICommit(GitHubError):
    """A commit HRI's repository does not have on the branch or tag it is asked against (a fork's, one that never was,
    or the branch or tag is gone): never downloaded."""


class GitHub:
    def __init__(self, cache_path: str | None, token: str = "", api_url: str = API_URL, codeload_url: str = CODELOAD_URL,
                 session: aiohttp.ClientSession | None = None):
        self._cache_path = cache_path
        self._token = token
        self._api = api_url.rstrip("/")
        self._codeload = codeload_url.rstrip("/")
        self._session = session
        self._own_session = session is None
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def _load_cache(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = {"fetched_at": 0, "releases": []}
            if self._cache_path:
                try:
                    with open(self._cache_path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict) and isinstance(data.get("releases"), list):
                        self._cache = {"fetched_at": float(data.get("fetched_at") or 0), "releases": data["releases"]}
                except (OSError, ValueError, TypeError):
                    pass
        return self._cache

    def _save_cache(self) -> None:
        if not self._cache_path:
            return
        tmp = self._cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh)
            os.replace(tmp, self._cache_path)
        except OSError as err:
            _LOGGER.warning("release cache not saved: %s", err.strerror)

    def cached_releases(self) -> list[dict]:
        return list(self._load_cache()["releases"])

    def cache_age(self) -> float | None:
        fetched = self._load_cache()["fetched_at"]
        return time.time() - fetched if fetched else None

    async def releases(self, refresh: bool = False) -> list[dict]:
        """HRI's releases from 0.25.0 on, newest first: {tag, version, prerelease, published_at, url, name}."""
        async with self._lock:
            cache = self._load_cache()
            age = time.time() - cache["fetched_at"]
            if cache["fetched_at"] and (age < CACHE_TTL and not refresh or age < MIN_REFRESH):
                return list(cache["releases"])
            try:
                raw = await self._fetch_releases()
            except GitHubError:
                if cache["releases"]:
                    _LOGGER.warning("GitHub unreachable: using the release list from %d s ago", int(age))
                    return list(cache["releases"])
                raise
            self._cache = {"fetched_at": time.time(), "releases": parse_releases(raw)}
            self._save_cache()
            return list(self._cache["releases"])

    def _api_headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _api_json(self, path: str, cap: int, what: str) -> Any:
        data = await self._get(f"{self._api}/repos/{names.HRI_REPO}/{path}", self._api_headers(), cap, 30)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GitHubError(f"GitHub's {what} is not JSON") from None

    async def _fetch_releases(self) -> Any:
        return await self._api_json("releases?per_page=100", MAX_RELEASES_JSON, "release list")

    async def release(self, tag: str) -> dict | None:
        """One release of HRI by its tag (releases/tags/<tag>), parsed as in the list; None when there is no such
        published release (a draft, or older than 0.25.0, counts as none)."""
        if names.version_from_tag(tag) is None:
            return None
        try:
            data = await self._api_json(f"releases/tags/{quote(tag, safe='._-')}", MAX_RELEASES_JSON, "release")
        except GitHubError as err:
            if "not found" in str(err):
                return None
            raise
        parsed = parse_releases([data])
        return parsed[0] if parsed and parsed[0]["tag"] == tag else None

    async def resolve_ref(self, kind: str, ref: str) -> str | None:
        """Check that a branch or tag exists in HRI's repository; its commit, when GitHub names it directly (an
        annotated tag names a tag object instead: None)."""
        names.validate_ref(kind, ref)
        full = names.full_ref(kind, ref)
        try:
            data = await self._api_json(f"git/ref/{quote(full[len('refs/'):], safe='/._-')}", 64 * 1024, "ref")
        except GitHubError as err:
            if "not found" in str(err):
                raise GitHubError(f"{names.HRI_REPO} has no {kind} {ref}") from None
            raise GitHubError(f"{kind} {ref}: {err}") from None
        obj = data.get("object") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("ref") != full or not isinstance(obj, dict):
            # git/ref/<x> answers only an exact ref; anything else is not the ref asked for
            raise GitHubError(f"{names.HRI_REPO} has no {kind} {ref}")
        sha = obj.get("sha")
        return sha if obj.get("type") == "commit" and isinstance(sha, str) and names.SHA_RE.fullmatch(sha) else None

    async def _get(self, url: str, headers: dict, cap: int, timeout: float) -> bytes:
        try:
            async with self._get_session().get(url, headers=headers, allow_redirects=False,
                                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 404:
                    raise GitHubError("not found on GitHub")
                if resp.status != 200:
                    raise GitHubError(f"GitHub answered HTTP {resp.status}")
                return await read_capped(resp, cap)
        except TooLarge:
            raise GitHubError("GitHub's answer is larger than the manager accepts") from None
        except asyncio.TimeoutError:
            raise GitHubError(f"no answer from GitHub in {int(timeout)} s") from None
        except aiohttp.ClientError as err:
            raise GitHubError(f"GitHub unreachable: {err.__class__.__name__}") from None

    async def commit_on_ref(self, sha: str, kind: str, ref: str) -> None:
        """NotHRICommit unless ``sha`` is ``ref`` (a branch or tag of HRI) or an ancestor of it, by HRI's own repository:
        compare/<ref>...<sha> answers "behind" (sha is behind the ref) or "identical"."""
        names.validate_ref(kind, ref)
        if not isinstance(sha, str) or not names.SHA_RE.fullmatch(sha):
            raise NotHRICommit("not a commit")
        full = names.full_ref(kind, ref)
        # the base by its full name (refs/heads/... or refs/tags/...), never a short one that a branch and a tag could
        # share: GitHub's compare accepts it, as checked against its API (behind or identical for an ancestor)
        try:
            data = await self._api_json(f"compare/{quote(full, safe='/._-')}...{sha}", MAX_RELEASES_JSON, "comparison")
        except GitHubError as err:
            if "not found" in str(err):
                raise NotHRICommit(f"{names.HRI_REPO} has no {kind} {ref}, or no commit {sha[:12]}") from None
            raise
        status = data.get("status") if isinstance(data, dict) else None
        if status not in ("behind", "identical"):
            raise NotHRICommit(f"commit {sha[:12]} is not on {names.HRI_REPO}'s {kind} {ref} ({status})")

    async def tarball_of_commit(self, sha: str, kind: str, ref: str) -> tuple[tarsafe.Archive, str]:
        """The source of a commit the manager recorded when it built an instance from HRI's branch or tag (Repair of
        a git instance at its installed commit): checked to be on that branch or tag first, then to be that commit."""
        await self.commit_on_ref(sha, kind, ref)
        url = f"{self._codeload}/{names.HRI_REPO}/tar.gz/{sha}"
        try:
            data = await self._get(url, {}, tarsafe.MAX_COMPRESSED, 300)
        except GitHubError as err:
            raise GitHubError(f"commit {sha[:12]}: {err}") from None
        archive = await asyncio.to_thread(tarsafe.open_archive, data)
        if archive.sha != sha:
            archive.close()
            raise GitHubError(f"the archive of commit {sha[:12]} names another commit")
        return archive, url

    def tarball_url(self, kind: str, ref: str) -> str:
        names.validate_ref(kind, ref)
        return f"{self._codeload}/{names.HRI_REPO}/tar.gz/{quote(names.full_ref(kind, ref), safe='/._-')}"

    async def tarball(self, kind: str, ref: str) -> tuple[tarsafe.Archive, str]:
        """The source of a branch or tag of HRI, checked (tarsafe) and opened; with the URL it came from."""
        url = self.tarball_url(kind, ref)
        try:
            data = await self._get(url, {}, tarsafe.MAX_COMPRESSED, 300)
        except GitHubError as err:
            raise GitHubError(f"{kind} {ref}: {err}") from None
        # unpacking and checking an archive of up to 64 MiB compressed takes a while: not on the event loop
        return await asyncio.to_thread(tarsafe.open_archive, data), url


def parse_releases(raw: Any) -> list[dict]:
    out = []
    for rel in raw if isinstance(raw, list) else []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        version = names.version_from_tag(rel.get("tag_name"))
        if not version or not names.supported_version(version):
            continue
        url = rel.get("html_url") if isinstance(rel.get("html_url"), str) and rel["html_url"].startswith(names.HRI_URL + "/") else names.HRI_URL + "/releases"
        out.append({
            "tag": rel["tag_name"], "version": version, "prerelease": bool(rel.get("prerelease")) or any(c.isalpha() for c in version),
            "published_at": str(rel.get("published_at") or ""), "url": url, "name": str(rel.get("name") or rel["tag_name"])[:200],
        })
    out.sort(key=lambda r: names.parse_version(r["version"]), reverse=True)
    return out


def latest_stable(releases: list[dict]) -> dict | None:
    return next((r for r in releases if not r["prerelease"]), None)
