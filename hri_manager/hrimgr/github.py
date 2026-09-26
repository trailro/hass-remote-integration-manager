"""HRI's releases and source tarballs, from GitHub.

Two fixed hosts, no redirects followed: api.github.com for the release list (with the optional token, for the rate
limit) and codeload.github.com for tarballs (never with the token: HRI is public).  A ref is validated
(names.validate_ref) and escaped before it becomes part of a URL.  The release list is cached in /data for an hour,
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

    async def _fetch_releases(self) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self._api}/repos/{names.HRI_REPO}/releases?per_page=100"
        data = await self._get(url, headers, MAX_RELEASES_JSON, 30)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GitHubError("GitHub's release list is not JSON") from None

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

    def tarball_url(self, ref: str, tag: bool = False) -> str:
        names.validate_ref(ref)
        path = f"refs/tags/{ref}" if tag else ref
        return f"{self._codeload}/{names.HRI_REPO}/tar.gz/{quote(path, safe='/._-')}"

    async def tarball(self, ref: str, tag: bool = False) -> tuple[tarsafe.Archive, str]:
        """The source of a tag (``v0.25.0``) or of any ref, checked (tarsafe) and opened; with the URL it came from."""
        url = self.tarball_url(ref, tag)
        try:
            data = await self._get(url, {}, tarsafe.MAX_COMPRESSED, 300)
        except GitHubError as err:
            raise GitHubError(f"{'tag' if tag else 'ref'} {ref}: {err}") from None
        return tarsafe.open_archive(data), url


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
