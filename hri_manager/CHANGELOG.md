# Changelog

## 0.1.3

Fixes from a second external review (of 0.1.2). Still **experimental**.

- **Decoys elsewhere in the local apps folder.** The Supervisor's store reads every `config.*` of the whole local apps
  folder and keys each app by the slug inside the file, the last one found winning: a file in any other folder that
  declared `slug: hri_<name>`, with its own image or a map of Home Assistant's configuration (neither reported by the
  Supervisor), could be installed and started by the manager's own Install or Update and listed as healthy. The
  manager now searches the folder by the Supervisor's own rule, refuses to create, install, update, rebuild, repair or
  start while such a file is there, checks again right after an install or update (and contains what was installed
  if one appeared), and lists every decoy with its path under Other HRI apps.
- **A new definition is checked against what the manager wrote**, not taken from the folder when the build returns: a
  file planted during the build (a `Dockerfile.<arch>`, a `config.yaml` with another image) refuses the write. The
  build folder is `0700`. Install of a restored definition refuses a `Dockerfile.<arch>`, `build.*` or `apparmor.txt`.
- **Checked right after the install or update too**, not only before: a change in the folder around it is contained.
- **A same-version change is flagged.** The registry records the sha256 of the `config.yaml` the manager wrote; another
  one is flagged like a foreign version (the Supervisor's own Rebuild applies a same-version definition unchecked),
  with Repair. Instances last written by 0.1.2 get the sha256 at their next write.
- **Containment:** an app that cannot be uninstalled gets start at boot turned off (the allow-list accepts `boot:
  manual` for this, with the instance's marker); a mark the registry cannot record is kept in memory and in the
  instance's marker for the next start; every job reads the mark again before it acts; the start's check of an app
  installed while the manager was away holds it when its folder cannot be checked or a decoy of it is there. A held
  app (stopped until it is checked) gets start at boot turned off too, and on again once Check again or Repair passes.
- **Bluetooth is never taken from the installed app** as your choice unless you choose it (an Update with Bluetooth
  chosen) or the manager made the change; Repair stops at needs attention otherwise. Following the app writes the
  registry first.
- **Unfinished updates** are settled only on what is known: an unreadable registry, or a broken new marker while the
  Supervisor has the new version, leaves both definitions; an Update after a start that could not ask the Supervisor
  settles the previous definition first; an update after one recorded late waits for its Check again; a registry that
  cannot be written reports as such (a successful update is a warning, never an unexpected error).
- **Create waits** for an interrupted install that may still finish (as Forget does), and a rollback that could not
  ask the Supervisor says what it left.
- **Robustness:** the background loop runs supervised (an unexpected error is logged, shown on the status and the
  loop starts again) and one odd folder (no `backup_exclude`, YAML nested too deep) no longer stops it; the folder
  walk goes by descriptor and never through a swapped link; a template's slug is never spelled out before it is
  vetted; no job starts once the stop began (503), and the sessions close after the jobs.
- **Docs:** the security model says plainly what the checks guarantee and what they cannot (someone who can write the
  local apps folder can still install things through the Supervisor's own buttons), lists `host_dbus`, and DOCS.md
  warns against the Supervisor's own Update and Rebuild buttons.
- **Smaller fixes:** duplicate `allowed_users` entries are not reported as blank; "repaired", not "repaird"; a marked
  row offers no Repair it would refuse; refused archives are closed at once; one rule for translation names; copies
  half-written by a stop are tidied at the start.
- **Code review of 0.1.3 before its release:** a `config.*` that is a link is always a decoy (the Supervisor resolves it
  from its own mount); a git build's Dockerfile is patched from the archive, never read back from the folder; before
  an install or update the store is made to read the folder again (a decoy removed with its date put back stayed in
  the store); a file that vanishes while the folder is searched is skipped, and a search that cannot finish holds
  the app instead of uninstalling it; a hold or containment keeps a start at boot you had turned off, and Check again
  does not turn it on; a file the manager cannot read blocks installs and updates, with its path and what to do, but
  not the start of a checked instance.
- **Who is trusted:** the README and DOCS.md say plainly that anyone who can write the local apps folder as root is
  fully trusted, and that the manager's checks of the folder are defence in depth, best effort against that person.

## 0.1.2

Fixes from an external review of 0.1.1, and Bluetooth per instance. Still **experimental**.

- **It installs only what it wrote.** Anyone who can write the local apps folder could change an instance's
  `config.yaml` between the manager's write and the Supervisor's reading of it, and the manager's own install or
  update would then install it (`hassio_role: admin`, full access...). The manager now records every file it writes
  (content, inode and change time, so a file changed and put back is caught too) and checks them before each store
  reload, after the store read it and right before the install or update; it compares the store's definition with
  what it stamped before, and the installed app's after, also before Finish setup, Repair or an update the Supervisor
  had already made take an app over. A difference refuses the install or update, or marks the instance first and
  then stops and uninstalls the app (its `/config` kept); if a broken marker blocks that, the job and the row say
  plainly that it was NOT uninstalled and what to do. A marked instance is never started, installed, updated or
  repaired. A field the Supervisor no longer reports stops and marks the app without uninstalling it.
- **Git definitions are installed only from a fresh download**, never from the tree left in the local apps folder;
  the copy in `/data` is kept from the bytes the build wrote, never read back from the folder; a FIFO in the folder no
  longer blocks the manager.
- **Bluetooth, per instance.** A box of the New instance form (and of the Update and Rebuild dialogs, applied with the
  new version) gives the instance the host's D-Bus (`host_dbus: true`) for an integration that talks to Bluetooth
  devices. Off unless chosen; HRI's template can never turn it on. See the README's Bluetooth section.
- **Ready for HRI 0.25.2.** Its template adds `backup_pre` and `backup_post` (run by the Supervisor inside the
  instance's own container around a backup), which the manager now accepts as HRI wrote them (one line, at most 512
  characters); 0.1.1 refused them, so no instance could update to 0.25.2. Instances created from, or updated to, HRI
  0.25.2 or newer keep HRI's own backups in their Home Assistant backup.
- **Never a downgrade after an interrupted update.** When the Supervisor has installed an update the manager stopped
  waiting for, the new definition is kept and recorded (then checked), never the previous one put back, which the
  Supervisor would offer, and auto-update install, as a downgrade.
- **A definition the manager did not write is flagged.** Every list compares the version the store offers with the
  one the manager recorded; another one is flagged ("do not update from the Supervisor's app page"), with Repair to
  write the manager's definition again. Never update an instance with the Supervisor's own Update button.
- **Marks with a way out:** an app the manager could not check can be checked again (Check again, Repair); a
  containment a stop cut short says "interrupted" and is done again at the next start; Bluetooth follows the
  installed app when Repair, Finish setup or an Update to the installed version write its definition.
- **An update killed midway is put back** at the manager's next start (the registry marks an update in progress
  before the swap); only the manager's records failing after a successful update is a warning, not a failure, and the
  next start records it.
- **An install call without a clean answer** (a timeout, a lost connection) keeps the instance, as install
  interrupted, instead of forgetting it while the Supervisor goes on installing; Forget waits an hour for it.
- **Stopping** cancels the jobs first and waits at most 5 s for open requests, within a 30 s stop timeout
  (`timeout: 30`), so a rollback always has its time. A file that cannot be tidied at the start no longer stops the
  manager from starting.
- **Safer writes:** every file below a definition folder is created through folder descriptors opened part by part
  without following links; the registry's folder is synced after each write; a refused build never leaves a registry
  entry behind; a refusal of HRI's template never spells a YAML value out (nested aliases took exponential time), and
  the copy in `/data` is checked before it is kept.
- **Administrators:** `allowed_users` matches a user's id or login name, never the display name (any administrator can
  change it); a list whose entries are all blank refuses everyone; a hanging Home Assistant is asked once for all
  waiting requests, and not again for 5 s. `allowed_users` is not a boundary between administrators, and the docs say
  so.
- **Bluetooth badge** shows what the installed app has, and an update the Supervisor already made records it.
- **Smaller fixes:** "newer release" counts the installed version too; a row keeps all its problems; an uninstall
  always says whether the data goes; archives whose names clash are refused before anything is written, and closed
  after use; versions take ASCII digits only; Delete says "delete" when it fails; the wait for the store is measured
  by the clock.
- **Release and CI:** the image is built from a lock of every dependency with its hashes
  (`hri_manager/requirements.txt`, compiled from `requirements.in`); the release build checks out the tag by its full
  name; the app-version job keeps no token in the checkout; its script fails clearly on a version it cannot read; the
  Supervisor checkout of CI is pinned by commit.

## 0.1.1

Fixes from a first run on a real Home Assistant OS 18.3 (Supervisor 2026.09.2). Still **experimental**.

- **Creating, installing, updating and repairing instances work on a real Supervisor.** 0.1.0 waited for the
  store's `version` of the new definition, but the Supervisor's `version` there is the installed app's (empty before
  the install); the definition's is `version_latest`. So every create failed after 90 seconds. The fake Supervisor of
  the tests now answers as a real one, checked against answers captured from one.
- **Repair only for the manager's own instances.** A detached `local_hri_*` app that the manager's registry does not
  hold (one made by hand) was offered Repair, and was then the manager's, deletable with its data. It is now listed
  as not managed, with no action.
- **A copy of each instance's definition** in the manager's `/data`, which is in its backups: Repair writes a release
  instance back with nothing downloaded, and a git instance from the source of its installed commit. At its start the
  manager copies the definitions it has no copy of.
- **Automatic repair after a restore.** A full backup leaves the local apps folder out (a Supervisor issue), so a
  restore leaves the instances detached. When the manager starts, and at most every 5 minutes when its page lists the
  instances, each instance of its registry in that state gets its definition written again; the log and the
  instance's row say so. Nothing outside the registry is touched.
- **A published image.** The manager's image is built for amd64 and aarch64 and pulled from
  `ghcr.io/trailro/hass-remote-integration-manager`, so its backups no longer hold a locally built image. The app's
  version on the store moves only once that version's image is published and pullable.
- **Rebuild onto the installed commit.** A git instance restored at another commit than the one the registry
  recorded needs attention, and Repair refuses it; a Rebuild onto a branch or tag whose current commit is the
  installed one was refused too ("Repair writes its definition"). It now writes that commit's definition (the
  installed version: nothing to update) and records that branch or tag and commit, after Repair's checks.
- An automatic repair that finds an instance **needs attention** no longer promises a next try that never comes: the
  log and the row give the reason and the actions (Update or Rebuild, Delete, Repair), with no retry note.
- **Repair keeps an instance's history.** The rewritten marker said `created_by: "automatic repair"` and had lost its
  history and `updated_by`. The registry now keeps them, and a repair writes them back with a `repaired (automatic)`
  or `repaired (manual)` entry added; an Update or Rebuild of an instance that needs attention keeps them too, and
  adds itself as any update does. An instance created by 0.1.0 gets its history in the registry at its first
  update by this version; a repair before that starts a new one.
- A local build of HRI (`local_hass_remote_integration`) is labelled as such, not as the published app.
- Docs: turn on **Show in sidebar** after installing the manager (the Supervisor leaves it off); one integration per
  instance, and two instances of the same integration cannot share an MQTT broker.

## 0.1.0

First release, **experimental**.

- Creates instances of [hass-remote-integration](https://github.com/trailro/hass-remote-integration) (HRI 0.25.0
  or newer) as local apps, each with its own sidebar panel, options, data and backups: pick a name and a release,
  the manager writes HRI's own app definition for it, installs it, turns on start at boot, the Watchdog and the
  panel, and starts it.
- Updates an instance to a newer HRI release; the app keeps its options and data. A failed update, or one stopped
  by the manager's own stop, puts the previous definition back; a create is rolled back the same way, and what is
  left over is offered as Finish setup, Install or Repair.
- Starts, stops, restarts and deletes instances; deleting needs the instance's name typed, always removes its
  options (the Supervisor does) and keeps its `/config` folder unless you ask.
- Repairs an instance whose definition folder is missing (on current Supervisors, a full backup leaves the local
  apps folder out): only a detached app the store does not define, from the manager's own registry of instances.
- A git channel, for testing: builds HRI from a branch or tag of its own repository on the device (checked to exist
  there first; never a commit, a pull request or a fork). That branch's code and Dockerfile run on the device.
- Lists the published hass-remote-integration app, and other local apps named like instances, without touching
  them.
- Security: the manager role is restricted in code to an allow-list of Supervisor calls, and only apps whose
  folder carries the manager's marker, matching its own registry, are ever changed. HRI's app template is vetted
  against a list of known keys and values before it is stamped. The UI is served only through Home Assistant's ingress, and
  only to Home Assistant's administrators: the app asks Home Assistant on every request and refuses when it cannot
  tell.
