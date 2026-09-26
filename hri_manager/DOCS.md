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

- `allowed_users`: the Home Assistant user names that may use the manager's panel. Empty: every Home Assistant user
  who can open it (the panel shows for administrators).
- `github_token`: optional, raises GitHub's rate limit for the release list. A token without any scope is enough.
- `debug`: more detail in the log.

## What the manager may do

The app has the Supervisor's **manager** role, which could stop or remove any app. The manager's code allows itself
only the calls listed in the README's security section, and changes only the apps it created (their folder carries
its marker). Its UI is reachable only through Home Assistant.

Full documentation: [README](https://github.com/trailro/hass-remote-integration-manager/blob/main/README.md).
