"""The manager's own list of the instances it created, in its ``/data`` (``instances.json``).

The marker in an instance's folder says the folder is the manager's, but anyone who can write the local apps folder
(a Samba or SSH user, another app that maps it) can write a marker.  Nobody but the manager writes its ``/data``.  So
every instance gets a random ``instance_id`` when it is created, written both in its marker and here, and an action
that changes an app needs the two to agree (``children.Managed.verify``): a folder with a marker and no entry here is
shown as not managed and gets no action.

``/data`` is in the manager's own backups, unlike (on current Supervisors) the local apps folder, so after a full
restore the registry still knows each instance's channel, branch or tag and commit, and Repair works from it.

An entry: name, slug, channel, version, ref_kind, ref, sha, instance_id, created_at, updated_at, setup_complete (the
create got as far as starting the app), stamp_version (stamp.STAMP_VERSION of the definition written), interrupted
(a create stopped by the manager's own stop); created_by, updated_by and history, the marker's, which a Repair writes
back into the marker; updating (an update between its swap and its record: children.cleanup_stale), tampered (the
Supervisor installed another definition than the manager's, which uninstalled it)."""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

FILE_NAME = "instances.json"
INSTANCE_ID_RE = re.compile(r"[0-9a-f]{32}")
MAX_FILE = 4 * 1024 * 1024


class RegistryError(Exception):
    """The registry cannot be read: nothing is treated as the manager's until it can."""


class Registry:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        try:
            with open(self.path, "rb") as fh:
                raw = fh.read(MAX_FILE + 1)
        except FileNotFoundError:
            return {}
        except OSError as err:
            raise RegistryError(f"the manager's registry cannot be read: {err.strerror}") from None
        if len(raw) > MAX_FILE:
            raise RegistryError("the manager's registry is larger than expected")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RegistryError("the manager's registry is not JSON") from None
        instances = data.get("instances") if isinstance(data, dict) else None
        if not isinstance(instances, dict) or not all(isinstance(v, dict) for v in instances.values()):
            raise RegistryError("the manager's registry has an unexpected shape")
        return instances

    def _write(self, instances: dict[str, dict]) -> None:
        """RegistryError when it cannot be written (a full or read-only /data): the one error callers expect."""
        try:
            tmp = f"{self.path}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "instances": instances}, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            # the rename itself is durable only once its folder is: without this a power loss can bring the previous
            # registry back, without an instance whose folder and marker survive (not managed, then)
            folder = os.open(os.path.dirname(self.path) or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        except OSError as err:
            raise RegistryError(f"the manager's registry cannot be written: {err.strerror or err}") from None

    def all(self) -> dict[str, dict]:
        with self._lock:
            return self._read()

    def get(self, name: str) -> dict | None:
        return self.all().get(name)

    def put(self, name: str, entry: dict[str, Any]) -> None:
        with self._lock:
            instances = self._read()
            instances[name] = dict(entry)
            self._write(instances)

    def update(self, name: str, **fields: Any) -> dict:
        with self._lock:
            instances = self._read()
            if name not in instances:
                raise RegistryError(f"{name} has no entry in the manager's registry")
            instances[name].update(fields)
            self._write(instances)
            return dict(instances[name])

    def remove(self, name: str) -> None:
        with self._lock:
            instances = self._read()
            if instances.pop(name, None) is not None:
                self._write(instances)
