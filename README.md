# HRI Manager

A Home Assistant app that runs several instances of
[hass-remote-integration](https://github.com/trailro/hass-remote-integration) (HRI) on one **Home Assistant OS**:
it creates, updates and removes them as local apps, one integration each.

HRI runs one Home Assistant integration in its own container and mirrors it to your Home Assistant over MQTT. On
Home Assistant OS it is an app with a single slug, so one install gives you one HRI. HRI Manager writes a copy of
HRI's own app definition for every instance you ask for, under its own slug (`local_hri_<name>`), and drives the
Supervisor to install, configure, start, update and remove it. Each instance is then an ordinary app: its own
sidebar panel, **Configuration** tab, data folder, logs and backups.

HRI itself is unchanged and stays the source of truth: the manager takes each instance's definition from the HRI
release you choose. On a plain Docker install you do not need this: run one HRI container per integration.

**Status: 0.1.0, experimental.** Home Assistant OS only (the Supervisor's local apps folder is required).

## Requirements

- Home Assistant OS with Supervisor **2026.07.1 or newer** (the `local_apps` folder mapping), Core 2025.10 or newer
  (HRI's own floor).
- HRI **0.25.0 or newer** for instances: the first HRI release that runs as an app with a sidebar panel.
- Internet access to GitHub (the release list and the source of the release you install) and to ghcr.io (HRI's
  image, pulled by the Supervisor).

## Install

1. **Settings > Apps > App Store**, menu **⋮ > Repositories**, add
   `https://github.com/trailro/hass-remote-integration-manager`.
2. Install **HRI Manager**. The Supervisor builds it on your machine (a small Python image); this takes a minute.
3. Start it and open **HRI Manager** in the sidebar.

## Creating an instance

Under **New instance**, give a name and pick a release:

- the name: lowercase letters, digits and `_`, starting with a letter, at most 20 characters. It becomes the app
  `local_hri_<name>`, shown as *HRI &lt;Name&gt;*, with the sidebar panel *HRI &lt;name&gt;*. It must not be the
  slug of any installed app; `manager` and a few other words are reserved;
- the release: HRI releases from 0.25.0 on, newest first; pre-releases are marked.

The manager then, as a job whose log the page shows:

1. downloads that release's source from GitHub and takes its `app/` folder (config, docs, translations);
2. writes the instance's definition to `hri_<name>/` in the local apps folder, stamped for this instance (below),
   with a marker file `.hri-manager.json`;
3. reloads the Supervisor's store and waits for `local_hri_<name>` to appear;
4. installs it (the Supervisor pulls HRI's image), turns on **Start on boot**, the **Watchdog** and **Show in
   sidebar**, and starts it.

If a step fails, what was done is undone: the app is uninstalled (its data folder kept), the definition removed and
the store reloaded.

The first start of an instance installs Home Assistant inside it and takes a few minutes; open its panel to follow
it, then set it up as the [HRI documentation](https://github.com/trailro/hass-remote-integration) says. Options
(password, `ingress_users`, Debian packages, ...) are on the instance's own **Configuration** tab.

### What is stamped

Only what makes the copy a separate app; everything else is HRI's, as released:

| Key | Instance `garage` |
|---|---|
| `slug` | `hri_garage` (the Supervisor calls it `local_hri_garage`) |
| `name` / `panel_title` | `HRI Garage` / `HRI garage` |
| `version` | the release's own, `0.25.0` (at a release tag HRI's `app/config.yaml` still names the previous version) |
| `ports` | `8087/tcp: null`: no port published. The panel works through ingress; map a port on the instance's **Network** tab if you want one |
| `backup_exclude` | `*_hass_remote_integration/…` becomes `*_hri_garage/…`, so the instance's backups leave out its installed Home Assistant (about 800 MB) as HRI's do |
| `webui` | dropped |

### Git channel (testing)

**Git ref (testing)** builds HRI from any branch, tag or commit instead of a release: for trying a fix before it is
released. The manager downloads that ref's source tree into the instance's folder, puts HRI's app definition at its
root without `image:` (so the Supervisor builds the folder's Dockerfile on your machine, which takes several
minutes), removes every other `config.yaml/yml/json` and `repository.*` of the tree (the store would read each as
another app), and gives it the version `0.0.0-<commit>`. HRI's page shows the commit as its build. **Rebuild**
downloads the ref again and rebuilds when the commit changed. Not for production.

## Updating

**Update** on an instance offers the releases at or above its version (the newest stable preselected; the page marks
an instance for which a newer release exists). The manager writes the new release's definition, keeping the previous
one aside, and asks the Supervisor to update the app: it pulls the new image and restarts the app with the same
options and data. If the update fails, the previous definition is put back and the app keeps running the version it
had. The manager does not downgrade.

The Supervisor's own **Update** button on the instance's app page appears only while the manager is updating it:
updates go through the manager, which is what moves the definition.

HRI Manager itself is updated from the App Store like any app (an app cannot update itself).

## Deleting

**Delete** stops and uninstalls the instance and removes its definition. Its data folder
(`app_configs/local_hri_<name>` on the host: its Home Assistant, integration and configuration) stays, unless you tick
the box and type the instance's name. A new instance with the same name reuses a data folder that stayed.

## Backups

Each instance is an app, so Home Assistant backups include it like any other app: its data folder, minus what HRI's
`backup_exclude` leaves out (stamped for the instance). The instance's **definition** is in the local apps folder,
which a full backup includes too.

- **Full backup restore**: instances, their definitions and the manager come back together.
- **Partial restore of an instance without the local apps folder**: the app comes back and runs, but its definition
  is missing, so the Supervisor calls it *detached* and it cannot be updated. The manager lists it with **Repair**,
  which writes the definition again for the installed version (for a release instance; for a git instance, when its
  commit can still be downloaded).
- The manager's own data (`/data`: the release list cache) needs no backup.

## Other HRI apps

The published single app (`<repository>_hass_remote_integration`) and local apps whose slug looks like an instance
but that the manager did not create are listed under **Other HRI apps**, read-only. Adopting the single app into the
manager is on the roadmap.

## Security model

HRI Manager needs the Supervisor's **manager** role (`hassio_role: manager`): it is what installing, configuring,
updating and uninstalling apps takes. That role could do much more: stop, reconfigure or uninstall **any** app, read
other apps' options (their passwords among them), restart or shut down the host, create, restore and delete backups.
The Supervisor does not narrow it, and Home Assistant shows the app's security rating lowered for it. So the manager
narrows itself, in code, and the tests pin it:

- **One choke point with an allow-list.** Every Supervisor request goes through one function
  (`hrimgr/supervisor.py`, `SupervisorClient.call`) which refuses, before anything is sent, every method and path
  that is not one of these:

  | Call | Why |
  |---|---|
  | `GET /addons` | the installed apps (names, versions, states; no options) |
  | `GET /addons/self/info`, `GET /supervisor/info`, `GET /info` | the manager's role and the versions shown in the page |
  | `POST /store/reload`, `GET /store/addons/local_hri_<name>` | make the store read a new or changed definition, and wait for it |
  | `POST /store/addons/local_hri_<name>/install` and `…/update` | install and update an instance |
  | `GET /addons/local_hri_<name>/info` | an instance's state and panel |
  | `POST /addons/local_hri_<name>/options` | only `boot: auto`, `watchdog` and `ingress_panel`: never the instance's own options |
  | `POST /addons/local_hri_<name>/start`, `…/stop`, `…/restart`, `…/uninstall` | the actions on an instance; `uninstall` only with `remove_config` |

  Paths must be plain (no `..`, escapes, queries); an app's slug is accepted only in the `local_hri_<name>` form;
  a request body may carry only the keys and values listed. Everything else (other apps, the host, backups,
  repositories, the v2 API) is refused.
- **Only its own apps.** A call that changes an app also needs proof that the manager created it: the marker file
  `.hri-manager.json` in `hri_<name>/`, naming that instance and the slug `local_hri_<name>`, read again from disk
  right before the call (never through a symlink). A `local_hri_*` app without it is left alone.
- **No secrets passed on.** The Supervisor includes an app's options in its info (an HRI instance's options hold its
  password); the manager keeps only a list of harmless fields and never shows or logs options. The Supervisor token
  and the optional GitHub token are never logged.
- **Files.** Writes stay inside `hri_<name>/` of the local apps folder: built in a hidden temporary folder and renamed
  into place, no symlink followed, YAML written with a safe dumper. Source archives from GitHub are checked before
  anything is written (no absolute paths, no `..`, no hard links or devices, links only to files of the same
  archive, size and count caps).
- **Network.** GitHub only: `api.github.com` (the release list, with the optional token) and `codeload.github.com`
  (source archives, never with the token); no redirect followed.
- **Administrators only, checked by the app.** Through Home Assistant's ingress only: the app publishes no port, and
  it serves a request only when the connection comes from the Supervisor (`172.30.32.2`, checked on the socket, not
  in a header). Being logged in to Home Assistant is not enough: any user may open an app's ingress, and the panel
  being hidden from non-administrators (`panel_admin`) is only cosmetic. So on every request, pages and API alike,
  the manager takes the user id the Supervisor's ingress sets (`X-Remote-User-Id`; a request with two ids, or two user
  names, is refused) and asks Home Assistant whether that user is an administrator: the command
  `config/auth/list` on Core's websocket, through the Supervisor's proxy (`homeassistant_api: true`, which costs no
  security rating), from which it computes `is_admin` as Core does (the owner, or an active member of the
  administrators group). A small client with its own allow-list (`hrimgr/corews.py`) can send only the
  authentication and that one command. The answer is cached for a minute. If Home Assistant cannot answer, the
  answer has an unexpected shape or the user is unknown, the request gets 403: the check fails closed.
  `allowed_users` narrows the administrators further. State-changing requests need `X-Requested-With: fetch` and a
  JSON body; the page's policy allows only its own scripts and styles and framing by Home Assistant. Like every
  ingress app, the panel shares Home Assistant's origin with other apps' panels.

What remains: the token itself has the manager role, so code running inside this app's container could use it. The
image contains only the manager (Python, aiohttp, PyYAML), built on your machine from this repository. And the
socket check trusts the hassio network: another app with the `NET_RAW` capability could spoof the Supervisor's
address there (ARP spoofing), a risk of the platform that every ingress app shares.

## Limitations

- Home Assistant OS only; one manager per system.
- Instances need HRI 0.25.0 or newer; the manager does not downgrade.
- The manager's image is built on your machine (no published image yet).
- Instances created by hand, and the single published HRI app, are not managed.

## Roadmap

- Published images for the manager (no build on the device).
- Adopting the single published HRI app as an instance.
- A helper to publish an instance's port.

## Development

The app is `hri_manager/`; its Python package is `hri_manager/hrimgr/` (aiohttp, PyYAML), the page is
`hrimgr/static/`. Tests use the standard library's `unittest` and aiohttp's test utilities, with a fake Supervisor
and a fake GitHub (`tests/fakes/stub.py`) reached over real HTTP:

```bash
python3 -m venv /tmp/hri-mgr-venv && /tmp/hri-mgr-venv/bin/pip install -r hri_manager/requirements.txt
/tmp/hri-mgr-venv/bin/python -m unittest discover -s tests -t .
```

`tools/dev_smoke.sh` builds the app's image and runs it next to the fake Supervisor in throwaway containers (the
manager in its development mode, which only environment variables the app cannot set turn on), drives create /
update / delete through the page's API and takes screenshots; see its header.

CI runs the unit tests on Python 3.13 and 3.14, the app linter on the manager and on an instance stamped from HRI's
template, the Supervisor's own schema checks of both (`.github/app_supervisor_check.py`, against a pinned Supervisor
release) and a build of the image.

## License

Apache License 2.0, as hass-remote-integration: see [LICENSE](LICENSE).
