# Changelog

## 0.1.0

First release, **experimental**.

- Creates instances of [hass-remote-integration](https://github.com/trailro/hass-remote-integration) (HRI 0.25.0
  or newer) as local apps, each with its own sidebar panel, options, data and backups: pick a name and a release,
  the manager writes HRI's own app definition for it, installs it, turns on start at boot, the Watchdog and the
  panel, and starts it.
- Updates an instance to a newer HRI release; the app keeps its options and data. A failed update puts the
  previous definition back.
- Starts, stops, restarts and deletes instances; deleting needs the instance's name typed, always removes its
  options (the Supervisor does) and keeps its `/config` folder unless you ask.
- Repairs an instance whose definition folder is missing (on current Supervisors, a full backup leaves the local
  apps folder out): only a detached app the store does not define, from the manager's own registry of instances.
- A git channel, for testing: builds HRI from a branch or tag of its own repository on the device (checked to exist
  there first; never a commit, a pull request or a fork). That branch's code and Dockerfile run on the device.
- Lists the published hass-remote-integration app, and other local apps named like instances, without touching
  them.
- Security: the manager role is restricted in code to an allow-list of Supervisor calls, and only apps whose
  folder carries the manager's marker are ever changed. The UI is served only through Home Assistant's ingress, and
  only to Home Assistant's administrators: the app asks Home Assistant on every request and refuses when it cannot
  tell.
