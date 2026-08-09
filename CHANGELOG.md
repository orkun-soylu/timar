# Changelog

Notable changes to Timar, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are
[SemVer](https://semver.org/spec/v2.0.0.html), with the caveat that the major version is `0`:
**while it stays there, anything may change between releases**, including the shape of
`config.yaml`. Read this file before upgrading.

## [Unreleased]

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

[Unreleased]: https://github.com/orkun-soylu/timar/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/orkun-soylu/timar/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/orkun-soylu/timar/releases/tag/v0.1.0
