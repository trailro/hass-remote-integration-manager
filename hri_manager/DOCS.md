# HRI Manager

Runs several instances of
[hass-remote-integration](https://github.com/trailro/hass-remote-integration) (HRI) on one Home Assistant OS, one
integration each. Every instance is a separate local app, `local_hri_<name>`, with its own sidebar panel, options,
data and backups.

Open **HRI Manager** in the sidebar:

- **New instance**: a name (lowercase letters, digits and `_`, at most 20) and an HRI release (0.25.0 or newer).
  The manager writes HRI's own app definition for it, installs it, turns on start at boot, the Watchdog and the
  sidebar panel, and starts it. The first start of an instance installs Home Assistant inside it, which takes a few
  minutes; open its panel to follow it.
- **Update** takes an instance to a newer release; its options and data stay. **Delete** stops and uninstalls it
  and keeps its data folder unless you tick the box and type its name.
- **Git ref (testing)** builds HRI from a branch, tag or commit on this machine. For trying a fix before its
  release; not for production.

Each instance's options (password, `ingress_users`, ...) are set on that app's own **Configuration** tab, as for the
single hass-remote-integration app. Its port 8087 is off; map one on its **Network** tab if you want it.

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
its marker). Its UI is reachable only through Home Assistant, and only by Home Assistant's administrators: the panel
is hidden from other users, and the app checks it on its side too, on every request (it asks Home Assistant, and
refuses when it cannot).

Full documentation: [README](https://github.com/trailro/hass-remote-integration-manager/blob/main/README.md).
