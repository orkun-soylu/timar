# Changelog

Notable changes to Timar, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are
[SemVer](https://semver.org/spec/v2.0.0.html), with the caveat that the major version is `0`:
**while it stays there, anything may change between releases**, including the shape of
`config.yaml`. Read this file before upgrading.

## [Unreleased]

Nothing has been tagged yet. Everything below is what exists on `main` today, and it is the
list a first release would be cut from.

> **Early.** The engine, the scheduler, the web UI and enrolment work and are covered by tests,
> but there is no upgrade path between commits and no stability promise. The whole installation
> is one directory (`/data`) — copy it before trying a new build.

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
