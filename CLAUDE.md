# CLAUDE.md — HRI Manager

A Home Assistant app (`hri_manager/`) that creates, updates and removes instances of hass-remote-integration (HRI)
as local apps. Python 3 + aiohttp, one process; the package is `hri_manager/hrimgr/`.

Operator-specific values (hosts, container names, local paths, test machines) live in `CLAUDE.local.md`, which is
gitignored. Never commit them here or anywhere else in this public repository.

## Hard rules (apply to every agent and subagent)
- No private infrastructure in the repo: no addresses except the Supervisor's `172.30.32.2`, loopback and the
  documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24); no host names, user names or home-folder
  paths. `tests/test_repo.py` checks part of this; grep the diff before every commit anyway.
- Never restart, stop, recreate or `docker exec` into the operator's production containers or Home Assistant
  (listed in `CLAUDE.local.md`), and never call their ports.
- Throwaway resources are named `hri-mgr-*` (containers, networks, volumes), publish on 127.0.0.1 only, and are
  removed at the end of the task even on failure.
- Never print tokens, passwords or cookies. The Supervisor token is the app's only secret besides the optional
  GitHub token.
- The Supervisor allow-list (`hrimgr/supervisor.py`, `RULES`) is the security core. Widening it needs a reason in
  the code, the pinned list in `tests/test_supervisor_allowlist.py` updated, and the README's security section.
- Every call that changes an app goes through a `children.Managed` (the marker check). Never add a path around it.
- Development mode (`HRI_MANAGER_DEV_*`) must stay impossible to reach from the app: no `environment:` key in
  `hri_manager/config.yaml`, no such variable in the Dockerfile.
- HRI stays the source of truth for an instance's definition: never copy HRI's config into this repo except the
  test fixture under `tests/fixtures/` (whose file names must not be `config.*`: the Supervisor would read it as a
  second app).

## Tests
```bash
python3 -m venv <scratch>/venv && <scratch>/venv/bin/pip install aiohttp==3.14.3 pyyaml==6.0.3
<scratch>/venv/bin/python -m unittest discover -s tests -t .
```
Keep venvs out of the repo. Check the exit code, never `| tail` the runner. All tests must pass before a commit.

`tools/dev_smoke.sh` builds the app and runs it next to the fake Supervisor (`tests/fakes/stub.py`) in throwaway
`hri-mgr-dev-*` containers; see its header.

## Release checklist
1. Version in `hrimgr/__init__.py` and `hri_manager/CHANGELOG.md` together (a test checks), and the README's status
   line. **Never `version` or `image:` in `hri_manager/config.yaml`**: the Image workflow's `app-version` job writes
   both, after it pushed that version's image and an anonymous pull of it works (`.github/workflows/image.yml`,
   `tests/test_image_workflow.py`). The store offers an update as soon as `main` names a new version; a version
   ahead of its image breaks every install and update (HRI had this bug). Until the first image exists the file has
   no `image:` line and the Supervisor builds the folder on the device.
2. CI green: unit tests, app linter, the Supervisor's own checks of the app and of stamped instances (schema, backup
   filter), the image built for amd64 and arm64.
3. Docs match the code.
4. Merge, then publish the GitHub release `vX.Y.Z` from `main`. The Image workflow checks that the tag's
   `__init__.py` says X.Y.Z, builds amd64 + arm64, pushes `ghcr.io/trailro/hass-remote-integration-manager:X.Y.Z`
   (`:X.Y` and `:latest` for the newest stable), checks an anonymous pull, then commits the version (and the first
   time the `image:` line) to `main`. Pre-releases get an image but never move the version.
5. The first release with an image only: the new ghcr.io package is private, so the anonymous-pull step fails and
   nothing moves. Make the package public (its settings, Change visibility), then re-run the failed `app-version` job.
6. Check the store offers the new version and that updating pulls the image instead of building.
