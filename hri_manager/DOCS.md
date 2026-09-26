# HRI Manager

Runs several instances of
[hass-remote-integration](https://github.com/trailro/hass-remote-integration) (HRI) on one Home Assistant OS, one
integration each. Every instance is a separate local app, `local_hri_<name>`, with its own sidebar panel, options,
data and backups. It needs Home Assistant OS with Supervisor **2026.07.1 or newer** (the local apps folder mapping).

After installing, turn on **Show in sidebar** on this app's **Info** tab: the Supervisor leaves it off for a new
app, so the sidebar has no **HRI Manager** entry until you do (**Open web UI** works meanwhile). Then open
**HRI Manager** in the sidebar (Home Assistant administrators only):

- **New instance**: a name (lowercase letters, digits and `_`, at most 20) and an HRI release (0.25.0 or newer).
  The manager writes HRI's own app definition for it, installs it, turns on start at boot, the Watchdog and the
  sidebar panel, and starts it. The first start of an instance installs Home Assistant inside it, which takes a few
  minutes; open its panel to follow it.
- **Update** takes an instance to a newer release; its options and data stay. If a create or update stops midway,
  the page offers what is left: **Finish setup**, **Install** or **Repair**. **Delete** stops and uninstalls it
  and needs its name typed. Its options (password, `ingress_users`) are always removed by the Supervisor; only its
  `/config` folder is kept, unless you tick the box. A new instance with the same name reuses that `/config` folder,
  without the old options: set them again.
- **Git branch or tag (testing)** builds HRI from a branch or tag of its own repository on this machine: that
  branch's code and Dockerfile run here. For trying a fix before its release, with a branch you trust; not for
  production. Commits, pull requests and forks are refused.

**Bluetooth** (a box of the **New instance** form) gives the instance the host's D-Bus (`host_dbus`), through
which BlueZ offers the host's Bluetooth adapters: for an integration that talks to Bluetooth devices. D-Bus is a
powerful interface that reaches many of the host's services, so turn it on only for an integration that needs it.
Home Assistant keeps using the same adapter (BlueZ serves several clients); the adapter recovery features that need
raw HCI sockets are not available in the instance. It changes only with a new version (the box in **Update** or
**Rebuild**); otherwise delete the instance keeping its data and create it again. See the README's Bluetooth section.

**One integration per instance.** Two instances running the *same* integration cannot share an MQTT broker: HRI's
base topic and client id are `hass_<domain>`, named after the integration and not a setting, so they would take each
other's connection and retained data. Give each instance a different integration (two config entries of one
integration go in one instance), or give each its own broker; per-instance base topics are a possible future HRI
feature.

Each instance's options (password, `ingress_users`, ...) are set on that app's own **Configuration** tab, as for the
single hass-remote-integration app. Its port 8087 is off; map one on its **Network** tab if you want it.

**Backups.** On current Supervisors a full backup leaves out the local apps folder, where each instance's definition
lives (an upstream issue). After a full restore the instances come back detached; the manager writes their
definitions again by itself, without anyone opening this page: it checks when it starts and then every 5 minutes,
so each instance is repaired within about 5 minutes of its own restore (or at once when the page lists it, or with
**Repair**), from the copy it keeps in its own `/data` (a release needs nothing from GitHub; a git instance
downloads the source of its installed commit), or from GitHub when it has no usable copy. Repair writes only the
installed version: when its exact source cannot be had, nothing is written and the instance is marked **needs
attention**, with **Update**/**Rebuild** (a newer version, written and installed at once; a Rebuild onto a branch
or tag whose current commit is the installed one writes that commit's definition and installs nothing) and **Delete**
offered.
Keep the manager in the same backups: its `/data` holds the registry and the copies that Repair works from.

## Options

- `allowed_users`: narrows who may use the manager. The check keys on the user id Home Assistant's ingress gives
  (verified with Home Assistant): an entry matches that id, or the login name Home Assistant reports for it
  (compared without case); never the display name, which any administrator can change. The user name a request
  carries is never trusted for this. The manager is always for administrators only, whatever this option says: a
  user who is not one gets 403 even with the panel's address. Empty: every administrator. Entries that are all blank
  refuse everyone (the log says so) instead of admitting every administrator. It is **not a security boundary between
  administrators**: any administrator can edit this option (the app's Configuration tab) and change other users'
  login names, so it only narrows which of them use the panel day to day.
- `github_token`: optional, raises GitHub's rate limit for the release list. A token without any scope is enough.
- `debug`: more detail in the log.

## What the manager may do

The app has the Supervisor's **manager** role, which could stop or remove any app. The manager's code allows itself
only the calls listed in the README's security section, and changes only the apps it created (their folder carries
its marker, and its own registry in `/data` agrees). Its UI is reachable only through Home Assistant, and only by Home Assistant's administrators: the panel
is hidden from other users, and the app checks it on its side too, on every request (it asks Home Assistant, and
refuses when it cannot).

Full documentation: [README](https://github.com/trailro/hass-remote-integration-manager/blob/main/README.md).
