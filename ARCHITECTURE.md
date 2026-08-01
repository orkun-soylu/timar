# Timar — Architecture & Design Rationale

Agentless care for a homelab fleet: wake machines that are asleep, update them, read their
logs, put them back the way they were found. Nothing is installed on the managed hosts — the
only requirement is SSH.

This document records *why* things are the way they are: the decisions, the measurements behind
them, and the traps that are easy to fall into again. The code says what; this says why.

## Scope

- **Managed hosts are usually off.** On-demand machines are the normal case, not the exception,
  so waking and re-sleeping is part of every operation rather than a special mode.
- **Agentless.** SSH and Wake-on-LAN only. No daemon to install, no port to open on the target.
- **Single container.** Web UI, scheduler and notifier ship as one image with one `/data`
  volume, so the whole installation moves by copying a directory.
- **Single operator.** This is an admin tool; it holds a key that reaches every host it manages.
  There are no roles to divide, only one account to protect.

## Platform command sets

The same question needs a different command per platform, and the wrong one usually fails
*quietly*. Every command in `platforms.py` was run against a real host before being written
down. Measured on OpenWrt 25.12.5 (busybox 1.37.0) and Debian 13:

| | OpenWrt | Debian |
|---|---|---|
| `df -h --output=pcent,target` | ❌ `unrecognized option` | ✅ |
| **`df -hP`** | ✅ six POSIX columns | ✅ six POSIX columns |
| `journalctl` | ❌ absent | ✅ |
| `logread` | ✅ | ❌ absent |
| `sudo` | ❌ absent (already root) | ✅ |
| package manager | `apk` (not `opkg` — 25.12 switched) | `apt` |

Three consequences, each of which is a test in `tests/test_platforms.py`:

- **`df -hP`, never `--output=`.** The GNU-only spelling is *rejected* by busybox, and a
  rejection is a non-zero exit that the original implementation swallowed into an empty result.
  That is a disk check which silently passes on every router it is pointed at. `-P` also forces
  one record per line; plain `df` wraps long device names and shifts every field.
  The fifth column is headed `Capacity` on busybox and `Use%` on coreutils, so the parser reads
  columns from the end rather than by name.

- **OpenWrt's `/rom` is always 100% full.** It is the read-only squashfs image; that is its
  normal state on every OpenWrt device, forever. Reported as a finding, it produces a critical
  disk alert on every run of every router — the fastest way to teach an operator to ignore this
  report. It is excluded by mount point. RAM-backed mounts (`tmpfs`, `devtmpfs`) are excluded on
  every platform for a related reason: a full tmpfs is a real problem, but a *different* one,
  and calling it "disk full" sends the operator to the wrong place.

- **OpenWrt has no default update command, deliberately.** `default_update_cmd` is `None`, and a
  host with no command resolves to `skipped` rather than an error. Unattended `apk upgrade` on a
  router can exhaust the overlay partition or land a kernel-module mismatch, and the machine
  that breaks is the one carrying the SSH session needed to repair it. An operator who wants
  this writes the command themselves.

> **The resolution happens before the host is woken.** For both hosts and Proxmox guests, the
> update command is resolved *first*; if there is nothing to run, the machine is never started.
> Booting a machine to do nothing is worse than useless, and on the guest path the early return
> that skipped the update would have skipped the matching shutdown too — leaving a VM running
> that the run had started itself.

`get()` falls back to plain Linux for an unknown platform id rather than raising: a typo in one
server's entry should cost that server the right commands, not abort the run for every other
server.

## Scheduled jobs are watched for absence, not errors

`log_checker._check_job_log` asks two questions about a job's log: did it start today, and did
it finish? Both matter, and the first matters more.

A job that fails writes an error line, and any log scan finds it. A job that stops being
*scheduled* writes nothing at all — no error, no output, no signal of any kind. Only the absence
of a run reveals it, which means something has to be looking for the absence.

The markers are configuration (`job_logs: [{path, started_marker, completed_marker}]`), not
constants. Any job that brackets its output with a recognisable start and finish line can be
watched this way.

## Open / next

- Web UI (FastAPI + templates), single container, `/data` volume
- SSH key generation, key push, and sudoers enrolment from the UI
- Provider-agnostic LLM for log analysis (currently Anthropic-only upstream)
- In-process scheduler with supervised tasks and a visible heartbeat
