# Changelog

Notable changes to Timar, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are
[SemVer](https://semver.org/spec/v2.0.0.html), with the caveat that the major version is `0`:
**while it stays there, anything may change between releases**, including the shape of
`config.yaml`. Read this file before upgrading.

## [Unreleased]

## [0.2.15] — 2026-10-02

### Added

- **Choose the servers a job runs on.** A job's dialog (the pencil on its row) lists, above
  *Save*: **Runs on** — each server with a **−** that asks, then leaves it out; **Left out** —
  enrolled servers taken out of this job, each with a **+**; **Not enrolled** — servers Timar has
  never logged in to with its key, each linking to its card's SSH section. Changes are saved at
  once and re-render only the lists, so a schedule being edited stays as typed. Each job has its
  own list (`job_exclude` in `config.yaml`); renaming or removing a server follows into it.
- **Enrolment is recorded.** Every login with Timar's key marks the address as enrolled
  (`ssh/enrolled`, beside `known_hosts`). An installation upgrading starts from `known_hosts`:
  every address Timar already pinned counts.

### Changed

- **Jobs run only on servers that are enrolled and not left out.** The others are not woken and
  not connected to. The update run names them as skipped with the reason — a hypervisor left
  out takes its VMs with it — and the log sweep's summary counts them ("2 not in the sweep").

## [0.2.14] — 2026-10-02

### Added

- **The containers table sorts by *Name* and *Host*,** like the servers table: click a heading,
  click again to flip it. Host sorts by server, then directory. The order is in the URL, so the
  15-second refresh, a reload and a bookmark keep it. The sortable heading is one macro
  (`_table.html`) shared by both tables.

## [0.2.13] — 2026-10-02

### Changed

- **The servers and containers pages open at once.** They waited for the fleet to be asked:
  every machine that was off cost a three-second connect timeout, and the containers page an
  SSH `docker ps` plus each health address in turn — measured on a 16-server fleet, 3.0s and
  0.9s whenever the 10-second cache had lapsed. A background task now asks every host, reads the
  containers and checks the health addresses every ten seconds, and the pages read what it found
  last: 0.002s for both. A request asks for itself only when there is no answer yet (right after
  a restart) or the last one is over a minute old. Waking, shutting down, starting or stopping
  still drops the cache, so the next refresh shows the truth.
- The connect probe gives an off machine one second instead of three — a machine that is up
  answers in milliseconds, through the VPN tunnel too.
- Health addresses are checked in parallel rather than one after another.

## [0.2.12] — 2026-10-02

### Changed

- **Each job is edited from its own row.** The **+** on *Scheduled work* suggested more jobs
  could be added; there are exactly two. It is gone, and the heading carries only its **?**, as
  every other heading does. Each row has a pencil beside *run*: it opens that job's settings,
  filled with what is saved — the log sweep's schedule with its log window and disk threshold,
  or the update run's schedule. Saving one job leaves the other untouched.

## [0.2.11] — 2026-10-02

### Changed

- **A report opens in a dialog.** The report button on a row of the reports list used to leave
  the page; it now opens the report over the list, in a dialog wide enough for its columns, and
  Esc or × brings the list back as it was. `/reports/<id>` still opens as a page of its own for a
  bookmark or without scripting.

## [0.2.10] — 2026-10-02

### Changed

- **The settings page carries no instructions of its own.** The API key and bot token boxes said
  "stored — leave blank to keep it"; they now say only *stored* or *not set*. What that means —
  never shown again, empty keeps it, typing replaces it, switching provider drops the key — is
  in the section's **?**, reworded to match what the box shows.

## [0.2.9] — 2026-10-02

### Fixed

- **The *Scheduled work* heading's controls line up.** The **?** sat 4.5px above the heading's
  centre line — its 1.5rem button was baseline-aligned next to the 2rem **+** — and to the left
  of it. It is now at the far right, where every other heading keeps its **?**, with **+** just
  inside, both centred on the line.

## [0.2.8] — 2026-10-02

### Changed

- **Every explanation on the settings page and in the schedule dialog is behind a "?".**
  *Model* and *Notifications* get a **?** on their headings; the paragraphs under them, the note
  on the API buttons and the footnote about `config.yaml` move inside. The schedule dialog's
  labels lose their parenthesised instructions (*At*, *Day*, *Every N hours*); what they meant is
  in each job's **?**.
- **The help says more.** Each provider's needs and default model, what an empty base URL
  means, that switching provider drops the key, and **what the model sees** — the sweep's
  findings and each server's context line go to the provider, so a model of your own keeps them
  on your network. For Telegram: what is sent, that a failed delivery does not fail the job,
  where the bot token and the chat ID come from. For schedules: how *daily*, *weekly* and
  *interval* fire (interval counts from the last run and catches up after downtime), how long
  the log window should be, and that RAM-backed filesystems are left out of the disk check.

## [0.2.7] — 2026-10-02

### Changed

- **Schedules live with the jobs they drive.** *Scheduled work* on the reports page has a **+**
  beside its **?**, like the servers table. It opens a dialog with one section per job — *Log
  sweep* (its schedule, plus the log window and the disk threshold) and *Update run* (its
  schedule) — saved together, validated together, and back to the reports page. A mistake
  writes nothing and keeps what was typed. The two panels left the settings page, which now
  holds the model connection and notifications.
- The add/edit dialog is one shared partial for the servers, containers and reports pages.

## [0.2.6] — 2026-10-01

### Added

- **A running job can be stopped.** While a job runs its row shows a red **stop** instead of
  *run now*; it asks first and says what will happen. The stop is cooperative: the job looks
  before each host and each compose project, finishes the step it is on — a command already
  running on a host is not cut, a machine woken for the run is still shut down — and goes no
  further. The row reads *stopping…* until then, and the summary and the report say the run was
  stopped and what was not reached.

### Changed

- **The reports page keeps its notes behind a "?".** *Scheduled work* and *Reports* each have a
  **?** on the right of their heading, opening what used to be the paragraphs under them, plus
  what the page's controls do.
- **The scheduled-work row carries only run (or stop).** The *report* and *history* buttons are
  gone; the full list is right below.

## [0.2.5] — 2026-10-01

### Changed

- **One heading style across the content area.** Panel titles (*Scheduled work*, *Reports*,
  the settings sections, a report's title, the add/edit dialog's title), form section titles
  and popover titles used three different sizes, weights and cases. They now all match the
  tables' column heads: small, upper case, muted. A titled panel's heading row has the same
  padding as a table's, so the two start alike.

## [0.2.4] — 2026-10-01

### Changed

- **The header stays on screen.** The menu bar is sticky: a long servers or containers table
  scrolls under it.
- **Action results appear in the header, right of the name, for three seconds.** Wake, shut
  down, start, stop and restart used to answer below the table — on a long page, off screen. The
  answer now shows beside *timar* and fades after three seconds; a second answer replaces the
  first and restarts the clock. A long one is cut with an ellipsis and shown whole on hover.

## [0.2.3] — 2026-10-01

### Added

- **Find on a host.** The add-container form opens with a host picker and **Find**. Timar asks
  that server for its compose projects (`docker compose ls -a` and `docker ps`, one SSH session)
  and lists those not registered yet. Each row shows a tick box, an editable name, the directory
  with Compose's status, and a web address guessed from a Traefik `Host()` rule on the project's
  containers. **Add selected** adds the ticked rows with the defaults — all of them, or, if any
  row is wrong (a name already taken, say), none, with the list coming back as it was sent.
  A project removed with `down` has nothing for Compose to list and is still added by hand.

## [0.2.2] — 2026-10-01

### Fixed

- **Pages no longer shift sideways when moving between them.** A page short enough to fit the
  window (the servers table) had no scrollbar, so its centred content sat about 15px to the
  right of a long page's, and the menu, the table's edges and its corners jumped on every
  switch. The scrollbar's room is now kept on every page (`scrollbar-gutter: stable`).

## [0.2.1] — 2026-10-01

### Added

- **The update run updates compose projects**, each right after its host and over the same
  connection, with a line of its own in the report. Per project, an *Update* section in its form:
  **pull + up** (default) — `docker compose pull --ignore-buildable`, then `up -d` only if the
  project was running; **custom command**, run in the project's directory; or **skip**.
  - Never `down` first: a failed pull leaves the project as it was rather than removed.
  - A project that was not running is pulled and left stopped ("pulled, left stopped").
  - Unused images are pruned once per host afterwards.
  - Timar's own project is always skipped, whatever its setting.
- **The log sweep knows on-demand projects.** Their stopped containers are no longer findings.
  The sweep reads `docker ps` as JSON now, so it can tell which project a container belongs to;
  stopped containers outside a registered on-demand project are reported as before.

## [0.2.0] — 2026-10-01

### Added

- **A containers page**, between *servers* and *reports*: Docker Compose projects on the fleet's
  servers, one row per project directory. The same shape as the servers page — a light before
  the name, the name a link to the web interface while it runs, the images it runs, and **start**,
  **stop**, **restart** and **edit** on the row, **+** on the heading.
  - State is one `docker ps -a` per host over SSH, grouped by the compose working-directory
    label, in parallel and cached; a host that is not up is not asked and its projects read as
    unknown. A finished one-shot (exit 0) does not degrade a project; a failed container, a
    failing healthcheck or an optional *health check* address that stops answering does.
  - Start, stop and restart are `docker compose up -d` / `stop` / `restart` in the project's
    directory, with `sudo -n docker` when the account is not in the `docker` group.
  - Timar's own project gets no stop or restart: it recognises its container by the id in
    `/proc/self/mountinfo`, and the action route refuses it too.
  - The add/edit form has the server form's shape: *Container info* and *Health check*
    sections with a **?** each, and *remove* — which forgets the entry, nothing more.
  - `containers:` in `config.yaml`; renaming a server carries into its containers.

## [0.1.22] — 2026-10-01

### Changed

- **Remove moved into the server form.** The row's trash button is gone; *remove* sits at the
  right end of the edit form's buttons, in red, and asks first ("Remove … from Timar? The
  machine is not touched."). A row now carries only its power button and *edit*.
- The *Actions* heading keeps the add button on its own line instead of letting it drop under
  the word once a row has two buttons.

## [0.1.21] — 2026-10-01

### Changed

- **The server form has three sections — Server info, Update, SSH access** — each under a plain
  heading on a rule, with a **?** on the right that opens everything worth knowing about it. The
  fields keep only their names and "optional"; the explanations moved into those popovers.
- **Enrolment is part of the form.** The *SSH access* section takes the SSH user, a password and
  *Also grant passwordless sudo*; **Enrol** saves the form and then installs Timar's key, proving
  it works with the key alone. With the password left empty, Enrol checks an existing
  enrolment instead (the old *Verify*). The password is still used for that one request only —
  never stored, never sent back, not even to a rejected form. Enter in a field saves; it never
  enrols. The separate enrol panel, its row button and its *Send magic packet* are gone (the
  row's wake button does that); `/settings/servers/<name>/enroll` now opens the form at
  *SSH access*.
- **One *On-demand* box** for every kind of machine. On a VM it is written to the hypervisor's
  `manages_vms` entry, as *Kept off on purpose — a VM you start only when needed* was; on a
  machine of its own, as `on_demand`. The edit form shows the effective state — a machine with a
  MAC, or a guest of an on-demand host, is ticked.
- *Guest of* offers **host** for a machine of its own instead of "not a virtual machine".
- The note under the servers table is gone; the state light's word is on hover.

## [0.1.20] — 2026-10-01

### Changed

- **The Linux default update command takes new packages.** It is now
  `apt-get -y -o Dpkg::Options::=--force-confold upgrade --with-new-pkgs`, then `autoremove`
  and `clean`. Plain `upgrade` held back every update that needs a package it does not have
  yet — kernel ABI bumps among them — so security kernels were never installed and nothing
  said so. `--force-confold` keeps an edited config file instead of stopping at a prompt.
- **The server form is shorter.** *Broadcast address* and *Wake through* are no longer form
  fields; they are advanced settings written in `config.yaml` (`wol_broadcast`, `wol_relay`),
  documented in the README under *Advanced wake settings*. The form notes when one is set, and
  saving it keeps them — they are no longer in `validate.SERVER_FIELDS`, so an edit carries
  them across.
- **"platform default" opens the defaults.** Next to *Update command*, a link opens a popover
  with each platform's default command, so leaving the field empty is no longer a guess. The
  README lists them too.

## [0.1.19] — 2026-10-01

### Security

- **Host keys are pinned by Timar's own trust-on-first-use policy** instead of paramiko's
  `AutoAddPolicy`. The behaviour is the same: a host never seen before is accepted once and
  written to `known_hosts`, and a known host presenting a different key is refused. But the rule
  is now explicit, and it's in one place, `ssh.new_client()`, which enrolment uses too instead of
  carrying two copies of it. It also clears the code-scanning alert that `AutoAddPolicy` raised
  each time those lines moved. New tests run a real SSH handshake to check the pin, the
  acceptance of the same key, and the refusal of a changed one.

## [0.1.18] — 2026-10-01

### Changed

- **One menu on every page.** The header reads *timar* — lowercase, in the mark's amber — then
  **servers · reports · settings**, the language and *sign out*. The page you are on is
  highlighted in amber; the others are plain links. It replaces the per-page titles and the
  "← dashboard" links. The globe before the language picker and the operator's name are gone.
- **Scheduled work moved to the reports page**, above the archive: the latest run of each job,
  then every run before it. The servers page is the fleet table alone.
- The servers page note no longer claims that only on-demand machines can be powered off
  (out of date since 0.1.15).

## [0.1.17] — 2026-10-01

### Added

- **SSH on a port other than 22.** The address takes a port after the host —
  `10.0.0.5:2222`, `host.lan:2345`, or `[fd00::5]:2222` for IPv6 — and every connection made
  from it uses that port: the status probe, enrolment, log sweeps, update runs, shutdowns and
  wake relays. Before, the port was fixed at 22, so a host whose sshd listens elsewhere, or one
  reached through a local port forward, could not be added at all.

## [0.1.16] — 2026-10-01

### Added

- **A server's web interface is a link from the dashboard.** An optional *Web interface* field on
  the server form (`web_url` in `config.yaml`) takes an IP or a hostname, with a port and a
  scheme if needed — `10.0.0.5:8006`, `https://router.lan`. While the machine is up, its name
  opens that address in a new tab; asleep or down, the name stays plain text. Without a scheme
  the address is opened as `https://`; only `http` and `https` are accepted.

## [0.1.15] — 2026-09-29

### Changed

- **Every running machine can be shut down from the dashboard**, not only the ones Timar can
  wake again. An always-on server, a hypervisor, a router or a board switched on by hand gets
  the button too; its confirmation says Timar cannot wake it and it stays off until someone
  switches it on by hand. The old refusal is gone — whether a machine may be powered off is the
  operator's decision.
- **A guest of an always-on host gets a wake button when it is down** — `qm start` through its
  hypervisor, as for any other guest.

## [0.1.14] — 2026-09-29

### Added

- **On-demand without Wake-on-LAN.** `on_demand: true` on a server — or *Kept off on purpose*
  in its form — marks a machine that is switched on by hand: a Wi-Fi board, a laptop. Off
  reads as *asleep*, the log analysis is told it is normal, and an update run **skips** it
  while it is off instead of failing on the missing MAC. It gets no power buttons, because
  Timar could not wake it again. Before, the only way to be on-demand was a `wol_mac` or a
  hypervisor, so such a machine was reported as down and failed every update run it missed.

### Security

- The language switch's `next` redirect is rebuilt as `/` plus the path, not passed through
  after a prefix check. `//host` now lands on a path of this site, and backslashes, whitespace
  and control characters (which browsers fold into `//host`) are refused (CodeQL #24).

## [0.1.13] — 2026-09-29

### Security

- The language cookie is written from the catalog's own key, never from the request. The value
  was already checked against the supported languages first, but request input no longer
  reaches a `Set-Cookie` header at all.

## [0.1.12] — 2026-09-29

### Changed

- **The browser tab shows Timar's mark** — the same icon as [timar.tools](https://timar.tools),
  inline on every page, so it needs no route and no extra request.

## [0.1.11] — 2026-09-29

### Changed

- *Address* and *Platform* in the server table keep their columns but left-align their text,
  like every other column.

## [0.1.10] — 2026-09-29

### Changed

- **The server table's layout:** *Server* hugs the left edge and *Actions* the right, each as
  wide as its content; *Address* and *Platform* share the space between them, centred.

### Fixed

- **`/health` answers `HEAD`.** Uptime monitors (homepage's `siteMonitor` among them) probe with
  `HEAD` first; it got a 405 on every check and had to retry with `GET`.

## [0.1.9] — 2026-09-29

### Changed

- **The server list sorts by *Server*, *Address* or *Platform*.** Click a heading; click it
  again to reverse. Addresses sort numerically (`10.0.0.9` before `10.0.0.10`), names after
  IPs. The order is in the URL, so it survives the ten-second refresh and a bookmark.
- **The *State* column is gone; the state is a light in front of the name** — green up, grey
  asleep, red down — with the word as the tooltip. The legend under the table says which is
  which.
- **Dialogs have a close button (×) at the top right.**
- **Reports:** each run opens from an icon button, under an *Actions* heading, in every view of
  the list.

## [0.1.8] — 2026-09-29

### Changed

- **Servers are managed from the dashboard.** Each row's *Actions* now has enrol, edit and
  remove beside the power button, and *+* on the heading row adds a server. Enrol, add and edit
  open in a dialog; remove asks first. The settings page keeps only the fleet-wide settings.
  Old `/settings?edit=`, `?enroll=` and `?add=` links still lead to the right place.
- **The server form's hints are shorter.**
- The scheduled work table has an *Actions* heading over its buttons.
- On a narrow screen the tables scroll sideways instead of cutting off their last column, and
  the header wraps.

### Security

- An old `/settings?edit=` or `?enroll=` link redirects only to a server that exists, by its
  stored name. The target used to be built from the query string; it could not leave the site,
  but request input no longer reaches a `Location` header at all.

## [0.1.7] — 2026-09-28

### Changed

- **Row actions are icon buttons of one size.** On the dashboard, *wake up* and *shutdown* in
  the server list and *report*, *history* and *run now* under scheduled work are square icon
  buttons; the word is still there as the tooltip and the accessible name. An always-on
  machine's cell is now empty instead of saying *n/a*, and the column is headed *Action*
  rather than *Power*.
- **The server list is sorted by name**, not by the order the hosts were added in.

## [0.1.6] — 2026-09-26

### Fixed

- **The language selector is in the header now**, beside *settings* on the dashboard and at the
  top right of the sign-in and setup pages. In 0.1.5 it sat at the foot of every page, which on
  the dashboard is below the jobs table, and it went unnoticed.

## [0.1.5] — 2026-09-26

### Added

- **The web interface speaks seven languages:** English, Türkçe, Deutsch, Français, Русский,
  日本語 and 中文. The browser's `Accept-Language` picks one on the first visit; the selector at
  the bottom of every page — setup and sign-in included — overrides it and is remembered in a
  cookie. Form errors and power messages follow the page. Reports, job summaries and Telegram
  messages stay in English: they are written by background jobs, for whoever reads them later.

## [0.1.4] — 2026-09-25

### Fixed

- **The written assessment no longer disappears when thinking uses up the budget.** Current
  Claude models think by default, and thinking shares `max_tokens` with the answer; at the old
  default of 4000 a long sweep could come back with no text at all, which was shown as an empty
  assessment. The default is now 16000, and a reply cut off at `max_tokens` is reported as an
  error naming `llm.max_tokens` instead of passing as empty. If you point `openai` at a local
  server with a small context window, set `llm.max_tokens` lower yourself.

### Changed

- The analyst prompt no longer asks for a fixed number of sentences; it asks for an assessment
  brief enough to read at a glance.

## [0.1.3] — 2026-09-25

### Added

- **A guest of an always-on host can be marked on-demand.** Guests inherit on-demand from their
  hypervisor, so a VM kept off on a host that never sleeps — a management VM started only when it
  is needed — was reported down every night, and the power page refused to stop it. Tick *kept
  off on purpose* on the guest (stored as `on_demand: true` on its `manages_vms` entry). It is
  never a default and never inherited: without it, a guest of an always-on host is still always
  on, so a crashed 24/7 VM is still reported as a crash.

## [0.1.2] — 2026-09-25

A bug-fix release: long reports were not reaching Telegram.

### Fixed

- **Long reports reach Telegram again.** A findings report is one `<pre>` block; once it grew
  past Telegram's 4096-character limit it was split on line boundaries, the first chunk ended
  inside an unclosed `<pre>` and the second began with a stray `</pre>`. Telegram rejected both
  (`can't parse entities`), so the report most worth reading — the long one — was the one never
  delivered. Each chunk now closes the tags still open at its end and the next reopens them;
  a line cut to fit no longer ends in half an HTML entity either.

## [0.1.1] — 2026-08-09

A bug-fix release, and one of the three is the reason it exists: **in 0.1.0 no scheduled run
ever fired.** If you installed 0.1.0 and left it to do its work, it did none — upgrade, then
look at your report archive to see what was actually run and what only appeared to be.

All three were found by running 0.1.0 against a real fleet rather than by reading it.

### Fixed

- **Scheduled runs now actually fire.** Daily and weekly schedules never ran once: the loop
  re-asked `next_run` at the moment a job came due, and that function answers "the next moment
  in the future" — so at 09:30 it answered tomorrow, every time. Only manually triggered runs
  ever happened. Nothing looked wrong, because *next run* on the dashboard kept updating every
  minute. Check your archive: if every report in it is at an odd time, none of them were
  scheduled. Interval schedules were unaffected.
- **A run the process was killed in the middle of is no longer reported as still running.**
  The status is written before the work starts and after it ends, so a container stopped in
  between left `running` in `state.json` for good — it survives restarts, because it lives in
  the volume. Startup now closes such a run out as failed, dated to when it *started* rather
  than to the restart, and archives it, so the gap in the series carries a reason. The error
  says the part that matters after an interrupted update: machines it woke were never shut
  down again.
- **Editing a server in settings no longer deletes the parts of its entry the form does not
  show.** Saving rebuilt the entry from the form alone, so `job_logs`, `watch_logs`, `ssh_key`
  and a hypervisor's `manages_vms` were dropped by an unrelated edit — silently, and in the
  direction nothing reports. A sweep that stops watching a backup log writes no error, because
  a job nobody watches produces none; and a hypervisor edited into forgetting its guests leaves
  a VM that is then treated as always-on, turning every night it correctly spends powered off
  into an outage report. If you edited a server through the UI, check those fields in
  `config.yaml`.

## [0.1.0] — 2026-08-03

The first tagged release, and the first published image. Everything below already worked before
this tag; it marks a point someone can install and stay on instead of tracking `main`.

> **Early.** The engine, the scheduler, the web UI and enrolment work and are covered by tests,
> but there is no upgrade path between versions and no stability promise — while the major
> version is `0`, the shape of `config.yaml` may change under you. The whole installation is one
> directory (`/data`): copy it before moving to a new version.

### Added — the engine

- **Wake** a machine with a magic packet, directly or through a **relay**: another enrolled,
  always-on machine that sends the packet from the target's own segment. A relay is the only
  way to wake a machine in a different subnet, and the answer to Wake-on-LAN not working from a
  bridge network.
- **Update** each machine with its platform's command, waking what is asleep and shutting back
  down whatever started off. Proxmox hosts orchestrate their guests: wake the hypervisor, start
  the guest, update, shut both back down in order.
- **Log sweep** — system log errors, disk pressure, stopped containers, and scheduled jobs on
  those machines that did not run. Watching for *absence* is the point: a job that stops being
  scheduled produces no error at all.
- **Platform command sets** for Linux/systemd, OpenWrt and Proxmox VE. A check that cannot run
  on a platform says so instead of quietly reporting all-clear. OpenWrt has no default update
  command on purpose.
- **Optional model summary** of a sweep, through Anthropic, any OpenAI-compatible endpoint, or
  Ollama. Without one, the raw findings are reported instead.
- **Telegram delivery** of reports, as a copy of the archive rather than the only place the
  findings exist.

### Added — the web UI

- **Dashboard** with three states, not two: `up`, `asleep` (on-demand and expected to be off)
  and `down`. Probes run in parallel behind a short-lived cache.
- **Power** from the row that reports the state: wake a sleeping machine, shut down a running
  one. Guests go through their hypervisor. An always-on machine is offered nothing — Timar will
  not shut down a machine it cannot wake again.
- **Scheduled work panel** whose load-bearing field is `last run`. A schedule that stops firing
  writes no error anywhere; a stale timestamp is the only thing that reveals it.
- **Report archive** under `/reports`, filtered by job, so a disk creeping upwards or an update
  failing every week is visible as a series rather than one snapshot.
- **Settings**, split into per-server and fleet-wide tabs, with enrolment as a panel under the
  server list.
- **Single-operator authentication**: bcrypt, a JWT in an httpOnly `SameSite=Lax` cookie, and
  every path redirecting to `/setup` until an account exists.

### Added — running it

- **One container, one volume.** `/data` is the entire installation — config, credentials,
  state, reports and the SSH key — so backup and migration are `cp -a`.
- **In-process scheduler** with supervised tasks and a visible heartbeat, so a crashed loop
  becomes visible instead of silently ceasing.
- **Enrolment from the UI**: one keypair per installation, installed on a host with the
  operator's password used once, optional passwordless sudo written only after `visudo`
  accepts the file, and host keys pinned on first sight.

[Unreleased]: https://github.com/orkun-soylu/timar/compare/v0.2.15...HEAD
[0.2.15]: https://github.com/orkun-soylu/timar/compare/v0.2.14...v0.2.15
[0.2.14]: https://github.com/orkun-soylu/timar/compare/v0.2.13...v0.2.14
[0.2.13]: https://github.com/orkun-soylu/timar/compare/v0.2.12...v0.2.13
[0.2.12]: https://github.com/orkun-soylu/timar/compare/v0.2.11...v0.2.12
[0.2.11]: https://github.com/orkun-soylu/timar/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/orkun-soylu/timar/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/orkun-soylu/timar/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/orkun-soylu/timar/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/orkun-soylu/timar/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/orkun-soylu/timar/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/orkun-soylu/timar/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/orkun-soylu/timar/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/orkun-soylu/timar/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/orkun-soylu/timar/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/orkun-soylu/timar/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/orkun-soylu/timar/compare/v0.1.22...v0.2.0
[0.1.22]: https://github.com/orkun-soylu/timar/compare/v0.1.21...v0.1.22
[0.1.21]: https://github.com/orkun-soylu/timar/compare/v0.1.20...v0.1.21
[0.1.20]: https://github.com/orkun-soylu/timar/compare/v0.1.19...v0.1.20
[0.1.19]: https://github.com/orkun-soylu/timar/compare/v0.1.18...v0.1.19
[0.1.18]: https://github.com/orkun-soylu/timar/compare/v0.1.17...v0.1.18
[0.1.17]: https://github.com/orkun-soylu/timar/compare/v0.1.16...v0.1.17
[0.1.16]: https://github.com/orkun-soylu/timar/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/orkun-soylu/timar/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/orkun-soylu/timar/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/orkun-soylu/timar/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/orkun-soylu/timar/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/orkun-soylu/timar/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/orkun-soylu/timar/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/orkun-soylu/timar/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/orkun-soylu/timar/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/orkun-soylu/timar/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/orkun-soylu/timar/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/orkun-soylu/timar/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/orkun-soylu/timar/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/orkun-soylu/timar/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/orkun-soylu/timar/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/orkun-soylu/timar/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/orkun-soylu/timar/releases/tag/v0.1.0
