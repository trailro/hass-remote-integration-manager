# HRI Manager

Runs several instances of
[hass-remote-integration](https://github.com/trailro/hass-remote-integration) (HRI) on one Home Assistant OS, one
integration each. Every instance is a separate local app, `local_hri_<name>`, with its own sidebar panel, options,
data and backups.

Open **HRI Manager** in the sidebar (Home Assistant administrators only):

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

Each instance's options (password, `ingress_users`, ...) are set on that app's own **Configuration** tab, as for the
single hass-remote-integration app. Its port 8087 is off; map one on its **Network** tab if you want it.

**Backups.** On current Supervisors a full backup leaves out the local apps folder, where each instance's definition
lives (an upstream issue). After a full restore the instances come back detached; **Repair** in the manager writes
their definitions again (a release from GitHub; a git instance from its branch or tag, which must still exist). Keep
the manager in the same backups: its `/data` holds the registry that Repair works from.

## Options

- `allowed_users`: narrows who may use the manager, by Home Assistant user name or user id (either, compared
  without case). The manager is always for administrators only, whatever this option says: it asks Home Assistant
  on every request, and a user who is not an administrator gets 403 even with the panel's address. Empty: every
  administrator. A user name counts only when the request carries exactly one; users without a Home Assistant
  login name (for example external logins) are matched by their id.
- `github_token`: optional, raises GitHub's rate limit for the release list. A token without any scope is enough.
- `debug`: more detail in the log.

## What the manager may do

The app has the Supervisor's **manager** role, which could stop or remove any app. The manager's code allows itself
only the calls listed in the README's security section, and changes only the apps it created (their folder carries
its marker, and its own registry in `/data` agrees). Its UI is reachable only through Home Assistant, and only by Home Assistant's administrators: the panel
is hidden from other users, and the app checks it on its side too, on every request (it asks Home Assistant, and
refuses when it cannot).

Full documentation: [README](https://github.com/trailro/hass-remote-integration-manager/blob/main/README.md).
