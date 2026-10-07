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

**Status: 0.2.1, experimental.** Home Assistant OS only (the Supervisor's local apps folder is required).

## What it looks like

The manager's page, with three instances: two of HRI releases and one git build.

![HRI Manager's page: the managed instances attic, garage and lab with their state, HRI version, channel and actions; the New instance form; a local build of HRI under Other HRI apps](docs/images/manager.png)

Each instance is HRI's own app, with HRI's page as its sidebar panel:

![The sidebar panel of the instance garage: HRI's Overview page, running the hri_probe test integration](docs/images/instance-panel.png)

and its integration's devices mirrored into Home Assistant over MQTT:

![The device of the instance garage in Home Assistant: its controls and activity, from MQTT](docs/images/core-device.png)

## Requirements

- Home Assistant OS with Supervisor **2026.07.1 or newer** (the `local_apps` folder mapping), Core 2025.10 or newer
  (HRI's own floor).
- HRI **0.25.0 or newer** for instances: the first HRI release that runs as an app with a sidebar panel; **0.26.0
  or newer** for an instance on the host's network ([Host network](#host-network)).
- Internet access to GitHub (the release list and the source of the release you install) and to ghcr.io (the
  manager's and HRI's images, pulled by the Supervisor).

## Install

1. **Settings > Apps > App Store**, menu **⋮ > Repositories**, add
   `https://github.com/trailro/hass-remote-integration-manager`.
2. Install **HRI Manager**. The Supervisor pulls its image, `ghcr.io/trailro/hass-remote-integration-manager`
   (amd64 and aarch64), so the manager's backups do not hold a locally built image (0.1.0 was built on your machine,
   and its backups carried that image as `image.tar`).
3. On its **Info** tab, turn on **Show in sidebar** (the Supervisor leaves it off for a newly installed app, so
   there is no sidebar entry until you do), then **Start** it.
4. Open **HRI Manager** in the sidebar (or **Open web UI** on its Info tab). It is for Home Assistant
   administrators only (see [Security model](#security-model)).

The manager and each instance it creates are apps in **Settings > Apps**:

![The Supervisor's apps list: HRI Manager (experimental), the instances HRI Attic, HRI Garage and HRI Lab, HRI's own app and the Mosquitto broker](docs/images/apps-list.png)

## Creating an instance

Under **New instance**, give a name and pick a release:

![The New instance form: the name kitchen, the Release channel, HRI 0.25.1 (latest), and the app name local_hri_kitchen it gives](docs/images/new-instance.png)

- the name: lowercase letters, digits and `_`, starting with a letter, at most 20 characters. It becomes the app
  `local_hri_<name>`, shown as *HRI &lt;Name&gt;*, with the sidebar panel *HRI &lt;name&gt;*. It must not be the
  slug of any installed app, nor give the host name of one (the Supervisor writes `_` as `-` in host names, so
  `local_hri_a-b` takes `a_b`); `manager` and a few other words are reserved;
- the release: HRI releases from 0.25.0 on, newest first; pre-releases are marked.

The manager then, as a job whose log the page shows:

1. downloads that release's source from GitHub and takes its `app/` folder (config, docs, translations);
2. records the instance in its registry (`/data/instances.json`) and writes the instance's definition to
   `hri_<name>/` in the local apps folder, stamped for this instance (below), with a marker file
   `.hri-manager.json`; a copy of the definition goes to its own `/data/definitions/<name>/` (see
   [Backups](#backups));
3. reloads the Supervisor's store and waits for `local_hri_<name>` to appear, and checks that the store's definition
   is the one it wrote (see [Security model](#security-model));
4. installs it (the Supervisor pulls HRI's image), checks the installed app the same way, turns on **Start on boot**,
   the **Watchdog** and **Show in sidebar**, and starts it.

If a step fails, what was done is undone: the app is uninstalled (its `/config` folder kept), the definition removed
and the store reloaded. The same when the manager itself is stopped midway (for at most 8 seconds, within the
Supervisor's stop timeout of 30). Three cases are left for you to finish, and the page says which:

- the Supervisor went on installing after the manager stopped waiting (the manager was stopped, or its install call
  got no clean answer: a timeout, a lost connection, a server error): the app is listed as **install interrupted**,
  with **Repair** (then **Finish setup** or **Delete**). Until it shows up, the manager keeps its registry entry, and
  **Forget** is refused for an hour after the interruption;
- the manager stopped after the install but before the options and the start: the instance offers **Finish setup**,
  which turns on start at boot, the Watchdog and the panel, and starts it (the manager's log names such instances when
  it starts);
- a definition without its app (restored alone) offers **Install** for a release. A git instance is never installed
  from the tree left in the local apps folder (others can write it, and a build runs its Dockerfile): **Delete** it,
  keeping its `/config` folder, and create it again from its branch or tag.

If an instance of the same name was deleted before with its `/config` folder kept, the new one reuses that folder
(its Home Assistant, integration and credentials) but not the old options: set its password and `ingress_users` again.

The first start of an instance installs Home Assistant inside it and takes a few minutes; open its panel to follow
it, then set it up as the [HRI documentation](https://github.com/trailro/hass-remote-integration) says. Options
(password, `ingress_users`, Debian packages, ...) are on the instance's own **Configuration** tab.

### One integration per instance

Each instance runs **one** integration, as HRI does: for a second integration, create a second instance. From HRI
0.26.0 each instance derives its MQTT identity from its app's slug, so two instances running the **same**
integration can share a broker: each publishes under a per-instance base topic (see HRI's `docs/mqtt.md` for the
exact rule), unless it had already published under the integration's plain name, which it keeps so that its
entities in the main Home Assistant stay as they are. With an older HRI release, everything an instance publishes is
named after the integration's domain (base topic and client id `hass_<domain>`, not a setting), so a second instance
of the same integration would take the first's broker connection and clear its retained data: one instance of an
integration per broker there (two config entries of one integration go in one instance, as HRI's README says). The
manager sets nothing for this either way.

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
| `host_dbus` | `true`, only for an instance created (or updated) with **Bluetooth**, below |
| `host_network` / `ingress_port` | `true` / `0`, only for an instance created (or updated) with **Host network**, below; otherwise no `host_network`, and HRI's own `ingress_port` (8087) |

Before stamping, the manager checks HRI's template against the keys it knows from HRI's own app (`hrimgr/stamp.py`,
`TEMPLATE_KEYS`) and a vetted range for each: `map` only the instance's own `app_config`, `image` only HRI's, `url`
only HRI's repository, option types without `device`, and so on. A template with any other key (`hassio_role`,
`full_access`, `docker_api`, `privileged`, `host_network`, `devices`, `apparmor`, `environment`...) or a value
outside its range (an `ingress_port` of 0, say) is refused, on both channels, with "HRI's app definition has &lt;key&gt;, which this manager version
does not accept; update the manager". `uart: true` is kept: HRI uses it for serial sticks. `backup_pre` and
`backup_post` (from HRI 0.25.2 on) are kept as HRI wrote them, one line of at most 512 characters each: the Supervisor
runs them only inside the instance's own container around a backup (`docker exec`), so they can do nothing the
instance's own image cannot.

### Bluetooth

An integration that talks to Bluetooth devices needs the host's Bluetooth adapter. In an app that adapter is reached
through BlueZ, the host's Bluetooth service, over the host's D-Bus, which the Supervisor gives an app with
`host_dbus: true`. HRI's published app does not ask for it, and neither does an instance unless you tick
**Bluetooth (access to the host's D-Bus)** when you create it. The manager then adds `host_dbus: true` to that
instance's definition and records the choice in its registry. HRI's template can never add it: `host_dbus` is not one
of the keys the manager accepts from it. The instance's row shows a **Bluetooth** badge.

- **What it enables:** the integration in that instance can scan for and connect to Bluetooth devices through the
  host's adapters, as Home Assistant's own Bluetooth integration does on the host.
- **Security:** D-Bus is a powerful interface. It reaches not just BlueZ but many of the host's system services (as
  far as their own D-Bus policies allow), so an instance with it can do much more on the host than one without. Turn it
  on only for an integration that needs Bluetooth, and only with code you trust.
- **Home Assistant keeps the adapter.** BlueZ serves several clients at once: Home Assistant Core goes on using the
  same adapter while the instance scans and connects through it. An adapter has a limited number of connection
  slots, which they share.
- **No adapter recovery in the instance.** Home Assistant's features that reset or recover a stuck adapter need raw
  HCI sockets (host network and extra capabilities), which an instance does not get. Home Assistant Core on the host
  still has them for its own use of the adapter.
- **Changing it later** is part of **Update** or **Rebuild**: their dialogs have the same box, and the change is
  applied with the new version. An update applies an installed app's definition only when the app's version changes
  (the Supervisor refuses an update to the same version), so a change without a newer release or a new commit is
  refused with that reason. To change it at once, **Delete** the instance keeping its `/config` folder and create it
  again with the same name and the other choice (set its options again, see [Deleting](#deleting)).

Around its installs and updates the manager compares the `host_dbus` the Supervisor reports with the registry's choice
(see [Security model](#security-model)). When it writes the definition of the version already installed (Repair,
automatic or not, Finish setup, an Update to the installed version) and the installed app has the other value, it
records what the app has only when the manager made that change itself (an update of its own recorded late) or when
you choose it: an **Update** at the installed version with that Bluetooth choice. Otherwise it writes nothing and says
why: the Supervisor's own Update of a definition someone changed would give the app the host's D-Bus too, and that is
not your choice. A Repair then stops at **needs attention** (an Update to a newer release, with Bluetooth chosen, or
Delete).

### Host network

In an app's own network (bridge networking, the default) the container does not see the LAN's multicast and
broadcast, so an integration that finds its devices by mDNS (zeroconf), SSDP or UDP broadcast finds nothing. Such an
integration needs the host's network, which the Supervisor gives an app with `host_network: true`. HRI's published app
does not ask for it, and neither does an instance unless you tick **Host network** when you create it. The manager then
adds `host_network: true` and `ingress_port: 0` to that instance's definition and records the choice in its registry.
HRI's template can never add either: `host_network` is not one of the keys the manager accepts from it, and its
`ingress_port` must be a port. The instance's row shows a **Host network** badge.

- **Its own port.** On the host's network every instance would listen on HRI's port 8087 of the host, and two
  instances (or HRI's own app with its port published) would clash. With `ingress_port: 0` the Supervisor picks a free
  port for the app from 62000-65500 when it installs or updates it, keeps it for that app, and sends the sidebar
  panel's requests there; HRI reads that port from the Supervisor when it starts. HRI does so from **0.26.0** on, so
  Host network is refused for an older release ("Host network needs HRI 0.26.0 or newer"), and for a git build whose
  branch or tag lacks it: the manager reads the downloaded tree's `entrypoint.py` for the line
  `APP_DYNAMIC_PORT = True` (it reads it, it never runs it).
- **What it enables:** the integration in that instance can find and reach devices on your LAN by mDNS, SSDP and
  broadcast, as Home Assistant Core does on the host.
- **Security:** the instance shares the host's network namespace: it sees and can use every network interface of the
  host, and whatever it listens on is on your LAN. That includes its own web port (the one the Supervisor picked):
  HRI refuses a request there that does not come through the sidebar panel unless the instance's app has a password
  (set it on its **Configuration** tab), and then asks for it as on a published port. The sidebar panel keeps working
  either way. A port mapped on the instance's **Network** tab does nothing on the host's network (the Supervisor
  publishes none). Home Assistant shows the app's security rating one lower, and the Supervisor does not start a
  host-network app at boot while its firewall rules for the Docker gateway are not active. Turn it on only for an
  integration that needs it, and only with code you trust.
- **Changing it later** is part of **Update** or **Rebuild**, as for Bluetooth: their dialogs have the same box, and
  the change is applied with the new version, never at the same one. A Rebuild of an instance with Host network onto a
  branch or tag without `APP_DYNAMIC_PORT = True` is refused, and says so: untick Host network in that Rebuild to
  build it without.

When the installed app's `host_network` differs from the registry's choice, the manager follows the same rules as for
Bluetooth: it records what the app has only when the manager made that change itself (an update of its own recorded
late) or when you choose it (an **Update** at the installed version with that choice); otherwise it writes nothing and
says why, and a Repair stops at **needs attention**. One case differs: **Finish setup** cannot turn Host network off in
place (the definition it would rewrite no longer holds HRI's own port); an **Update** at the installed version with
Host network off writes it from HRI's template, and so does a Repair.

### Git channel (testing)

**Git branch or tag (testing)** builds HRI from a branch or a tag of
[trailro/hass-remote-integration](https://github.com/trailro/hass-remote-integration) instead of a release: for
trying a fix before it is released. **A git build runs that branch's code, and builds its Dockerfile, on this
machine: use it only for testing, and only with a branch or tag you trust.** Only branches and tags of HRI's own
repository are accepted, by their short name (`main`, `fix/something`, `v0.26.0b1`): never a commit, a pull request
(`pull/…`: anyone can open one) or a `refs/…` path. The manager checks with GitHub's API that the branch or tag
exists in HRI's repository before it downloads anything, downloads it by its full name (`refs/heads/…` or
`refs/tags/…`), and records the commit it got.

The manager downloads that source tree into the instance's folder, puts HRI's app definition at its root without
`image:` (so the Supervisor builds the folder's Dockerfile on your machine, which takes several minutes), removes
every other file the Supervisor's store would read as an app (by its own rule: `config.*` ending in `.yaml`, `.yml`
or `.json`, such as `docs/config.example.yaml`, outside dot folders and `rootfs/`) and nothing else, and gives it the
version `0.0.0-<first 12 hex digits of the commit>`. A tree with `apparmor.txt`, `build.yaml/yml/json` or a
`Dockerfile.<anything>` at its root is refused: the Supervisor would use them to confine or build the app instead of
HRI's Dockerfile. HRI's page shows the commit as its build. **Rebuild** downloads the branch or tag again and
rebuilds when its commit changed. Not for production.

## Updating

**Update** on an instance offers the releases at or above its version (the newest stable preselected; the page marks
an instance for which a newer release exists). The manager writes the new release's definition, keeping the previous
one aside, and asks the Supervisor to update the app: it pulls the new image and restarts the app with the same
options and data. If the update fails, or the manager is stopped midway, the previous definition is put back; if the
manager is killed midway (no time to put it back), its next start does it: the registry marks an update in progress
before the swap, and a definition in place that the registry never recorded goes, the previous one comes back. When
only the manager's own records could not be written after a successful update, the job says so as a warning, and the
next start records it. The manager does not downgrade.

![The Update dialog of the instance garage: from 0.25.1, a version to choose, Cancel or Update](docs/images/update-dialog.png)

**Never update or rebuild an instance with the Supervisor's own buttons** (Update or Rebuild on its app page, or its
auto-update). They install whatever the store read from the local apps folder at that moment, without any of the
manager's checks: `hri_<name>/`, or a decoy elsewhere in the folder that declares the instance's slug (see
[Security model](#security-model)). Update shows while the manager is updating the instance (its new definition is in
place a moment before the install), and whenever the folder holds another version than the installed one; Rebuild
applies a definition of the same version at once. The manager's page flags a definition it did not write ("the store
offers a definition the manager did not write", or "config.yaml is not the one the manager wrote"), with Repair to
write the manager's definition again, and lists every decoy. Updates go through the manager's **Update**, which moves
the definition and checks what the Supervisor installs.

A release instance records the commit its release tag named. When the manager downloads the same tag again (a
Repair, or rewriting the definition) and the tag names another commit, it refuses and flags the instance "tag
moved": a release tag is not supposed to move.

A newer manager may stamp definitions differently. Each definition records the manager's stamping version, and
**Update** at the same HRI version rewrites a definition stamped by an older manager. An update applies an installed
app's definition only when the app's version changes (the Supervisor refuses an update to the same version), so the
new stamping reaches the running instance at its next HRI update, or, for a git instance, its next rebuild of a new
commit. The manager does not force a rebuild for it (its allow-list has no rebuild); the Supervisor's own Rebuild
button would apply it at once, but without the manager's checks.

HRI Manager itself is updated from the App Store like any app (an app cannot update itself).

## Deleting

**Delete** stops and uninstalls the instance and removes its definition; it always needs the instance's name typed.
What goes and what stays:

![The Delete dialog of the instance attic: what is removed and kept, the box to delete its /config folder too, and Delete disabled until the name is typed](docs/images/delete-dialog.png)

- the instance's **options** (its password, `ingress_users`, Debian packages...) are always removed: the Supervisor
  drops them with the app, whether or not you keep its data;
- its **`/config` folder** (`app_configs/local_hri_<name>` on the host: its Home Assistant, integration, credentials
  and configuration) is kept unless you tick **Also delete the instance's /config folder**;
- a new instance with the same name **reuses that `/config` folder**, but starts without the old options: set its
  password and `ingress_users` again before anyone else can open it.

If the app is already uninstalled (for example, after the manager contained a changed definition), Delete can remove
its definition and registry entry, but any retained `/config` folder stays. The dialog explains this and hides the
data-removal checkbox. An API request to delete data in that state fails before removing the definition, registry or
copy: the Supervisor has no installed app to uninstall with data removal. Remove any retained folder manually if
needed, or delete without requesting data removal to keep it.

## Backups

Each instance is an app, so Home Assistant backups include it like any other app: its `/config` folder, minus what
HRI's `backup_exclude` leaves out (stamped for the instance), and its options. Instances created from (or updated to)
HRI 0.25.2 or newer keep HRI's own backups in their Home Assistant backup (earlier templates left them out); the
instance's `backup_pre` and `backup_post`, HRI's, flag the backup to HRI inside the instance's own container while the
Supervisor copies the folder.

The instance's **definition** (`hri_<name>/` in the local apps folder) is another matter. On current Supervisors a full
backup **leaves the local apps folder out**: the backup still names the folder `addons/local`, while the Supervisor
has moved it to `apps/local` (an upstream issue of the Supervisor, not of this app). So:

- **After a full restore**, instances come back **detached**: installed and running, but without a definition, so they
  cannot be updated. The manager **writes their definitions again by itself**, whether or not anyone opens its page:
  it checks when it starts and then every 5 minutes (and whenever its page lists the instances). The Supervisor
  restores the apps one after another, usually the manager first, so each instance is repaired at the first check
  after its own restore: within about 5 minutes, or at once when the page is open. Every instance of its registry
  that is installed, detached and without its folder gets a repair job, run by "automatic repair", written to the
  manager's log, and shown on the instance's row. If one fails (GitHub unreachable, say), the row says why and when the next try is, and offers
  **Repair**; each further failure doubles the wait (5 minutes, 10, 20, ... at most a day), and only the first
  failure is a warning in the log. A repair that finds the instance **needs attention** (below) is not tried again:
  the log and the row say why and what you can do, with no next try. Apps that are not in the registry are never
  touched.

  ![The instance garage after a restore: its row says its definition was written again automatically, with the time](docs/images/auto-repair.png)
- **After a partial restore of an instance without the local apps folder**: the same.
- The manager's own **registry** of the instances it created (`/data/instances.json`: each instance's channel, branch
  or tag, commit and id) and a **copy of each definition** (`/data/definitions/<name>/`) are in the manager's
  `/data`, which **is** in the manager's own backup. Restore the manager with the instances and Repair works from
  them:
  - a release instance: its whole definition is in the copy (the stamped `config.yaml`, HRI's `DOCS.md`,
    `CHANGELOG.md` and translations; a few kilobytes), so Repair needs nothing from GitHub;
  - a git instance: the copy holds its stamped `config.yaml` and the commit it was built from, not the source tree
    (that would be megabytes); Repair downloads the source of that commit again, so the definition matches the
    installed app and nothing needs rebuilding. It first asks GitHub whether the commit is on HRI's own branch or
    tag the instance was built from (codeload serves forks' commits under HRI's name too), and refuses it otherwise.

  A repair keeps the instance's history (who created it, its updates) from the registry, and adds itself to it
  (`repaired (automatic)` or `repaired (manual)`); so does an Update or Rebuild of an instance that needs attention,
  recorded as any update. For an instance created by 0.1.0 the registry has that history
  from its first update by 0.1.1 on; a repair before that starts a new one.

  Repair uses a copy only when it is the copy of that instance (the registry's instance id and channel) at the
  installed version, and its config is one the manager writes and matches its recorded digest, when available.
  At its start the manager captures a missing copy only if the local config matches a recorded digest; old registry
  entries without one recover from upstream. Otherwise, or without a copy, Repair downloads from
  GitHub: a release from its tag; a git instance, the commit the manager recorded for it.

  **Repair writes only the installed version, on the installed channel.** The channel is read from the installed
  app itself (an image of an HRI release, or a build of a `0.0.0-<commit>` version) and must be the registry's; a git
  instance's recorded commit must be the installed version. A definition of any other version would be offered by
  the Supervisor as an update, and installed on its own with auto-update on. When that exact source cannot be had
  (the release was withdrawn, the commit cannot be downloaded, the registry disagrees with the app), nothing is
  written: the instance is marked **needs attention**, with the reason, and automatic repair leaves it alone. You
  choose: **Update** (a release: a newer release) or **Rebuild** (git: the current commit of its branch or tag),
  which writes that definition and installs it in one step (and removes it again if the Supervisor refuses the
  update); **Delete**; or
  **Repair** again once the cause is gone. A Rebuild onto a branch or tag whose current commit **is** the installed one
  (a restore brought back another commit than the recorded one) writes that commit's definition, installs nothing,
  and records that branch or tag and commit, after the same checks as Repair (the commit is on HRI's branch or tag,
  and is the installed version). GitHub being unreachable is not such a case: automatic repair tries again. The page
  offers Update or Rebuild of a detached instance only when it needs attention (Repair otherwise); the manager's API
  takes it for any detached instance of its registry, and writes and installs a newer version the same way.

  **A detached update waiting to be finished.** When such an update was sent but its answer was lost (or the manager
  stopped while the Supervisor worked on it), the Supervisor may still finish it, even after the manager restarts.
  The manager keeps the new definition and records the requested version, its source and the Bluetooth and Host
  network choices; the row says the update is waiting to be finished, and the API's row has its target in
  `pending_update`. As soon as the Supervisor reports that version, the next start (or **Check again**, or Repair
  once its folder is gone) records it. Until then **Check again** and an Update to any other version or with other
  choices are refused, and automatic repair leaves the instance alone. The ways out: **Update** retries exactly that
  update (the dialog offers only its version); **Repair**, while the Supervisor still has the version the update
  began with, gives the update up: the new definition goes and the definition of the installed version is written
  again (use it once the Supervisor's own log shows the update failed: if it still finished later, the installed app
  would no longer match its definition); **Delete**. An update the manager was stopped before it sent (a power cut,
  the manager killed) cannot finish: its next start removes the definition it wrote and the instance is detached
  again, as before.

**If the manager's `/data` is lost** (the manager uninstalled with its data, or a restore without the manager's
backup), its registry and its copies are gone. By design the manager then takes no instance for its own (anyone who
can write the local apps folder can write a marker): every instance is listed under **Other HRI apps** as not
managed, with no action, and keeps running as it is. To manage one again:

1. in **Settings > Apps**, uninstall the instance **without** deleting its data;
2. if its `hri_<name>/` folder is still in the local apps folder (the `addons` share over Samba, or `/addons` over
   SSH), delete that folder;
3. in the manager, **Create** an instance with the **same name**: it reuses the instance's `/config` folder
   (`app_configs/local_hri_<name>`: its Home Assistant, integration, credentials and configuration).

What is lost: the instance's options (password, `ingress_users`, Debian packages...; the uninstall drops them, set
them again before anyone else opens it), its sidebar, start-at-boot and Watchdog settings (the create turns them on
again), and the manager's history of it (created, updates, the stamping version). A git instance is created again
from the branch or tag you name.

Repair is offered only for an app the Supervisor reports as detached **and** that the manager's registry holds (an
instance this manager created), and runs only if, after a store reload, the store still has no definition of its
slug. A detached `local_hri_*` app the registry does not hold (one made by hand, say) is listed as not managed, with
no action: Repair would make it the manager's, and then deletable with its data.

## Other HRI apps

The published single app (`<repository>_hass_remote_integration`), a local build of HRI
(`local_hass_remote_integration`, labelled so), local apps whose slug looks like an instance but
that the manager did not create (detached or not), `hri_<name>/` folders whose marker the manager's registry does
not hold, and every other app definition in the local apps folder that declares an instance's slug (a decoy, with its
path: see [Security model](#security-model)) are listed under **Other HRI apps**, read-only: the manager offers no
action on them. So are the manager's
own records of an instance that is neither installed nor defined (uninstalled outside the manager, its folder gone):
its registry entry and its copy in `/data`. **Forget** (with the name typed) drops those records and touches nothing
else; it refuses while the app is installed or its folder exists, and for an hour after an install that was
interrupted (the Supervisor may still finish it). Adopting the single app into the
manager is on the roadmap.

## Security model

HRI Manager needs the Supervisor's **manager** role (`hassio_role: manager`): it is what installing, configuring,
updating and uninstalling apps takes. That role could do much more: stop, reconfigure or uninstall **any** app, read
other apps' options (their passwords among them), restart or shut down the host, create, restore and delete backups.
The Supervisor does not narrow it, and Home Assistant shows the app's security rating lowered for it. So the manager
narrows itself, in code, and the tests pin it.

**Who is trusted.** Anyone who can write the local apps folder as root (the Samba `addons` share, SSH, an app that maps
the folder) is **fully trusted**: that person can already install whatever they want through the Supervisor's own
buttons, and has links, modification times and timing on their side against anything the manager checks. The
manager's checks of the folder are defence in depth, best effort against that person: they catch accidents, simple
tampering and many deliberate attempts, and guarantee nothing against someone with root on the folder the Supervisor
reads. **Do not give that access to anyone you do not trust.** What does hold against everyone else (Home Assistant
users who are not administrators, the network, and the manager's own flows) is below: the allow-list, the marker and
registry, the administrator check, the secrets kept out of answers and logs.

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
  | `POST /addons/local_hri_<name>/options` | only `boot` (`auto`; `manual` for an app the manager contains and could not uninstall, or holds until it is checked, so the Supervisor does not start it again at the next boot), `watchdog` and `ingress_panel`: never the instance's own options |
  | `POST /addons/local_hri_<name>/start`, `…/stop`, `…/restart`, `…/uninstall` | the actions on an instance; `uninstall` always with `remove_config` (required: never the Supervisor's default) |

  Paths must be plain (no `..`, escapes, queries); an app's slug is accepted only in the `local_hri_<name>` form;
  a request body may carry only the keys and values listed, and must carry those a call requires. Everything else (other apps, the host, backups,
  repositories, the v2 API) is refused.
- **Only its own apps.** A call that changes an app also needs proof that the manager created it: the marker file
  `.hri-manager.json` in `hri_<name>/`, naming that instance and the slug `local_hri_<name>`, and the manager's own
  registry of the instances it created (`/data/instances.json`), which must hold the same random instance id. Both
  are read again right before the call (never through a symlink). Anyone who can write the local apps folder (Samba,
  SSH, another app) can write a marker, but not the manager's `/data`: a folder whose marker the registry does not
  hold is listed as not managed and gets no action, and a `local_hri_*` app without a marker is left alone.
- **What it installs is checked against what it wrote (best effort).** A writer of the local apps folder can change a
  definition after the manager wrote it and before the Supervisor reads it (the store reads the whole folder again at
  a reload), and the manager's own install or update would then install, say, `hassio_role: admin`. Around the
  installs and updates it makes, the manager looks:
  - **at the folder it builds.** A new or updated definition is built in a hidden folder (`0700`), and before it is
    put in place, what the folder holds is compared with what the manager wrote into it (names, and the content of
    each file); a git build's Dockerfile is patched from the downloaded archive, never read back from the folder. A
    file planted or changed during the build (a `Dockerfile.<arch>` the Supervisor would build instead of HRI's
    Dockerfile, say) refuses the write, when it is there at that moment.
  - **at the folder since.** For the files, folders and links of `hri_<name>/`, and the folder itself, the manager
    records a content hash, the inode and the change time, and compares them again before each store reload, after
    the store has read it, and right before and right after the install or update (read through folder descriptors,
    never through a link). The change time moves on a write, rename, link or permission change, so a `config.yaml`
    swapped for the Supervisor's reading and put back byte for byte is caught at the next look. Install of a
    definition the manager did not just write (restored without its app) takes the folder as it is, checked to be a
    definition this manager writes, without a `Dockerfile.<arch>`, `build.*` or `apparmor.txt` at its root.
  - **at the other folders.** The store reads each `config.*` (`.yaml`, `.yml`, `.json`) of the whole local apps
    folder, outside dot folders and `rootfs/`, and keys each app by the `slug:` inside the file, the last one found
    winning; the Supervisor does not report which file an app came from. So the manager searches the folder by the
    same rule. A decoy is a file that declares a slug of the manager's (`hri_…`, compared as host names), other than a
    folder's own `hri_<name>/config.yaml` declaring `hri_<name>`; so is any such file that is a link (the Supervisor
    resolves it from its own mount of the folder, where it may reach a file the manager cannot see), or that the
    manager cannot read (a FIFO, a file over 1 MiB). A local apps folder more than 40 folders deep, or with more than
    500,000 entries, cannot be searched to its end, and counts as one decoy that names no instance: move or remove
    what is too deep or too large there. While a decoy is there the manager creates, installs, updates,
    rebuilds or repairs nothing (each refuses before it writes, removes or reloads anything); one that names an
    instance also stops its Start. The page lists each with its path and what to
    do. Before the reload that precedes an install or update, the manager makes the folder's own modification time
    the newest (and refuses while a file is dated in the future), so the store reads the folder again instead of
    keeping what it read before.
  - **at what the Supervisor reports.** Before the install or update it compares the store's parsed definition
    (`GET /store/addons/local_hri_<name>`) with what it stamped, and after it the installed app's
    (`GET /addons/local_hri_<name>/info`): role, Supervisor, Core and auth APIs, full access, Docker API, host network,
    PID, IPC, UTS and D-Bus, privileges, devices, `uart`/`usb`/`gpio`/`video`/`audio`, kernel modules, AppArmor,
    ingress, ports, version, name, URL and whether it has an image. Neither answer reports `image` (which image, only
    whether there is one), `map`, `backup_pre` or `backup_post`; the store's answer also leaves out IPC, UTS, D-Bus,
    privileges, devices, the device flags, kernel modules and ports (`hrimgr/stamp.py`, `STORE_VIEW`). Those keys rest
    on the folder checks above alone, which see only what the folder holds when they look.

  A difference found before the install or update refuses it. One found after it (the folder changed or a decoy
  appeared around the install, or the installed app reports another definition) first marks the instance in the
  registry, then stops and uninstalls the app (its `/config` folder kept), and says so in the job, the log and the
  instance's row. If it cannot be uninstalled, its start at boot is turned off (unless you had turned it off
  yourself), so the Supervisor does not start it again at its next start. Stop, uninstall and that option go through
  the same allow-list, which needs the instance's marker: if the writer broke the marker, they are refused, and so are
  the manager's own Stop and Delete of that instance; the job and the row then say plainly that the app was NOT
  stopped or uninstalled, with what to do by hand (Settings > Apps). A marked instance is not started, installed,
  updated or repaired by the manager; while its marker is intact, Stop and Delete stay, and Delete (or Forget) clears
  the mark. When the registry cannot record a mark (a full `/data`), the manager refuses all the same while it runs,
  and leaves a trace in the instance's marker for its next start: best effort, since a writer of the folder can remove
  it. A field the Supervisor no longer reports is most likely a change of its API, and a search of the folder that
  cannot be finished is not a decoy found: the app is then held (stopped, its start at boot turned off unless you had
  done so, marked as not checked, kept installed; an uninstall would drop its options), and before an install or
  update the same refuses it. **Check again** on its row (or **Repair**, for an instance without its folder) checks it
  again and clears the mark when it passes, turning start at boot on again if the manager had turned it off; the
  manager also checks, at its start, an app installed while it was not watching (an update it had stopped waiting
  for), and holds it when its folder is not a definition this manager writes, or a decoy of it is there.
- **What the Supervisor's own buttons do.** The checks above look around **the manager's own** installs and updates.
  The Supervisor's own Update and Rebuild buttons on an app's page, and its auto-update, install whatever the store
  read, without them: the manager cannot stop that. What it does: on each list it compares, for each instance, the
  version the store offers and the sha256 of `hri_<name>/config.yaml` with what it recorded, and searches the folder
  for decoys; a definition it did not write is flagged, and it refuses to start, update or finish the instance until
  **Repair** writes its own definition again (or **Delete** removes the instance). It does not record what such an
  install gave the app as your choice (see [Bluetooth](#bluetooth) and [Host network](#host-network)).
- **Bluetooth (`host_dbus`) and Host network (`host_network`).** The only accesses to the host the manager itself adds
  to a definition, each only for an instance you created or updated with it, recorded in the registry; HRI's template
  can never add either. `host_dbus: true` gives the host's D-Bus, which reaches many of the host's system services,
  not only BlueZ (see [Bluetooth](#bluetooth)). `host_network: true` (with `ingress_port: 0`) puts the instance in the
  host's network namespace, its web port on the LAN, which HRI 0.26.0 and newer refuses there without the app's
  password; the manager allows it only for an HRI that listens on the port the Supervisor picks (see
  [Host network](#host-network)). Around its installs and updates the manager compares what the Supervisor reports
  with the registry's choices.
- **What the folder checks cannot do.** They see the local apps folder only when they look, and the Supervisor
  reports only part of a definition and not which file it read: a writer with root on the folder can change it between
  two looks (a decoy written and removed again, a file swapped after the last look), point a link at what only the
  Supervisor sees, or install what they want through the Supervisor's own buttons, and an app so installed gets
  whatever its definition asks for, within what the Supervisor allows any local app. The manager narrows what that
  person can do through it and says what it notices; it does not stop them (see **Who is trusted**, above). Root on
  the host can change anything and is outside this model.
- **No secrets passed on.** The Supervisor includes an app's options in its info (an HRI instance's options hold its
  password); the manager keeps only a list of harmless fields and never shows or logs options. A Supervisor error
  that quotes an app's options (invalid options: `… Got {…}`) is replaced by its error key and the app's slug. The
  Supervisor token and the optional GitHub token are never logged: the log formatter removes them from every line,
  tracebacks included.
- **Files.** Writes stay inside `hri_<name>/` of the local apps folder and the manager's own `/data`: built in a hidden
  temporary folder and renamed into place, no symlink followed (every file is created, read or removed through
  folder descriptors opened part by part with `O_NOFOLLOW`, the marker too, so a folder swapped for a link midway
  is refused), YAML written with a safe dumper. Source archives from GitHub are checked before
  anything is written (no absolute paths, no `..`, no hard links or devices, links only to files of the same
  archive, size and count caps; the gzip layer is unpacked as a capped stream first, so a huge tar header is refused
  too), outside the event loop.
- **Network.** Besides the Supervisor (and Core's websocket through it, for the administrator check below), GitHub
  only: `api.github.com` (the release list and refs, with the optional token) and `codeload.github.com` (source
  archives, never with the token); no redirect followed.
- **Administrators only, checked by the app.** Through Home Assistant's ingress only: the app publishes no port, and
  it serves a request only when the connection comes from the Supervisor (`172.30.32.2`, checked on the socket, not
  in a header). Being logged in to Home Assistant is not enough: any user may open an app's ingress, and the panel
  being hidden from non-administrators (`panel_admin`) is only cosmetic. So on every request, pages and API alike,
  the manager takes the user id the Supervisor's ingress sets (`X-Remote-User-Id`) and asks Home Assistant whether
  that user is an administrator. The Supervisor drops a client's own copy of that header only in its exact spelling,
  and a copy in another spelling (`x-remote-user-id`) replaces the Supervisor's value on the way; so the id header must
  arrive exactly once, and it and `X-Remote-User-Name` (at most once) exactly as the Supervisor spells them, or the
  request is refused. The question to Home Assistant is the command
  `config/auth/list` on Core's websocket, through the Supervisor's proxy (`homeassistant_api: true`, which costs no
  security rating), from which it computes `is_admin` as Core does (the owner, or an active member of the
  administrators group). A small client with its own allow-list (`hrimgr/corews.py`) can send only the
  authentication and that one command. The answer is cached for a minute, so an administrator who is demoted (or
  deactivated) in Home Assistant keeps access to the manager for up to 60 seconds. One question is asked at a time
  and every request waiting meanwhile takes its answer; a failed question is not asked again for 5 seconds. If Home
  Assistant cannot answer, the answer has an unexpected shape or the user is unknown, the request gets 403: the
  check fails closed. `allowed_users` narrows the administrators further, keyed on the verified id: an entry
  matches the id, or the login name Core reports for that id, never the display name (any administrator can change
  it); a list whose entries are all blank refuses everyone. The `X-Remote-User-Name` header is shown and logged,
  never trusted. `allowed_users` is **not a security boundary between administrators**: any Home Assistant
  administrator can edit the manager's options and change other users' login names, so it narrows which
  administrators use the panel, and nothing more. State-changing requests need `X-Requested-With: fetch` and a
  JSON body; the page's policy allows only its own scripts and styles and framing by Home Assistant. Like every
  ingress app, the panel shares Home Assistant's origin with other apps' panels.

What remains: the token itself has the manager role, so code running inside this app's container could use it. The
image contains only the manager (Python, aiohttp, PyYAML), built from this repository by its Image workflow and
published on ghcr.io. And the
socket check trusts the hassio network: another app with the `NET_RAW` capability could spoof the Supervisor's
address there (ARP spoofing), a risk of the platform that every ingress app shares.

## Limitations

- Home Assistant OS only; one manager per system.
- Instances need HRI 0.25.0 or newer, and 0.26.0 or newer on the host's network; the manager does not downgrade.
- Instances created by hand, and the single published HRI app, are not managed.

## Roadmap

- Adopting the single published HRI app as an instance.
- A helper to publish an instance's port.

## Development

The app is `hri_manager/`; its Python package is `hri_manager/hrimgr/` (aiohttp, PyYAML), the page is
`hrimgr/static/`. Tests use the standard library's `unittest` and aiohttp's test utilities, with a fake Supervisor
and a fake GitHub (`tests/fakes/stub.py`) reached over real HTTP:

```bash
python3 -m venv /tmp/hri-mgr-venv && /tmp/hri-mgr-venv/bin/pip install --require-hashes -r hri_manager/requirements.txt
/tmp/hri-mgr-venv/bin/python -m unittest discover -s tests -t .
```

`hri_manager/requirements.in` names the dependencies; `hri_manager/requirements.txt` is its lock, every package pinned
with its hashes (`pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in` in
`hri_manager/`), which the image, CI and the tests install. Dependabot compiles it again when it bumps a dependency.

`tools/dev_smoke.sh` builds the app's image and runs it next to the fake Supervisor in throwaway containers (the
manager in its development mode, which only environment variables the app cannot set turn on), drives create /
update / delete through the page's API and takes screenshots; see its header. Its fixed Docker resources, subnet
and host port are protected by `/tmp/hri-mgr-dev.lock`: concurrent smoke invocations stop before any Docker
cleanup. An abrupt kill can leave that lock; remove it only after confirming no smoke run is active.

CI runs the unit tests on Python 3.13 and 3.14, the app linter on the manager and on an instance stamped from HRI's
template, the Supervisor's own schema checks of both and of an instance with Bluetooth or Host network
(`.github/app_supervisor_check.py`, against a Supervisor release pinned by its commit; with its own backup filter over
three instances side by side: no venv or logs in any of them, their state kept, and HRI's own backups left out for an
instance of HRI 0.25.0's template and kept from HRI 0.25.2 on), a build of the image for amd64 and arm64, and, once `config.yaml` names an image, an anonymous
pull of that image at its version for both architectures (`.github/check_published_image.py`). A published release runs the Image workflow
(`.github/workflows/image.yml`): it pushes the image for both architectures to ghcr.io and only then moves the app's
version on `main` (see `CLAUDE.md`, Release checklist). Builds push only the exact version tag; a shared promotion
queue rechecks the stable releases before moving `:latest`, `:X.Y` and the app version, so an older build finishing
later cannot overwrite a newer promotion.

## License

Apache License 2.0, as hass-remote-integration: see [LICENSE](LICENSE).
