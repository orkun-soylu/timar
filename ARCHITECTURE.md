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

## The model is configuration, not a dependency

Log sweeps are summarised by a model the operator chooses: Anthropic, any OpenAI-compatible
endpoint (LM Studio, vLLM, llama.cpp, OpenRouter, OpenAI itself), or Ollama's native API.

The reason is adoption, not neutrality for its own sake. Requiring a cloud API key to use the
log analysis would put a paywall in front of a homelab tool — and the audience most likely to
run this is the audience most likely to already have Ollama on a machine in the same rack.
**No model configured is a fully supported state**: wake, update and the sweep all work, and
only the written assessment is absent. A model that is unreachable, out of credit or
misconfigured returns `None` and is logged; the findings are the product, the prose is
commentary on top, and commentary must not take the report down with it.

**Raw HTTP, no vendor SDKs.** Three SDKs, kept current, in one container image, to make one
request each — and they disagree about retries, response shapes and error types, which is the
disagreement `llm.py` exists to hide. `build_request()` and `extract_text()` are pure functions
per provider and are where the tests live; `complete()` is those two plus one `httpx` call.

Three differences that are easy to get wrong and are each held by a test:

- **Anthropic sends no sampling parameters.** `temperature`, `top_p` and `top_k` were removed
  on current Claude models and are now rejected with a 400. A client that builds one uniform
  body for every backend fails here, so the absence is asserted rather than left to convention.
- **`content[0]` is not the answer.** Thinking is on by default on current Claude models, so a
  thinking block can precede the text. Select the block by `type`, never by position.
- **An OpenAI-compatible endpoint with no key must get no `Authorization` header at all.** A
  local server is the common case, and some reject a bearer header with an empty token.

## Nothing about a particular fleet is in the code

`analysis.py` derives everything the model is told from configuration: the server list, which
machines are expected to be asleep, each server's `context` line, and the operator's
`notes.md`.

This is a correction, not a preference. The predecessor embedded one operator's machines —
names, addresses, GPU models — directly in the system prompt string. That made the prompt
unpublishable *and* made adding a server a code change. A test asserts the module contributes
no digits and no addresses of its own to the prompt.

**On-demand is derived from `wol_mac`, not a separate flag** — a machine with a wake address is
by definition one expected to sleep, so its being offline is stated as normal rather than
reported as an outage. The relationship is followed through `manages_vms` too: a guest started
by its hypervisor has no `wol_mac` of its own and would otherwise read as a permanently-on
machine that is down.

## The `/data` volume is the installation

```
/data/
  config.yaml     servers, schedules, LLM and notifier settings
  auth.json       the operator's username and password hash
  secret_key      signs session cookies
  state.json      last run results, heartbeats
  notes.md        operator-authored standing context for the log analysis
  ssh/id_ed25519  the key this installation presents to every managed host
```

Flat and non-configurable per-path on purpose: backup and migration are then `cp -a` of one
directory. **Nothing here ships in the image** — a fresh container starts with an empty volume,
and `load()` returning `{}` is the first-run state, not an error.

`write_private()` writes through a temporary file in the *same* directory and `os.replace`s it
into place. Same directory because `/tmp` is a separate mount inside the container and the
rename would cross filesystems; atomic because a half-written `config.yaml` is an installation
that will not start, and the web layer rewrites it while the scheduler may be reading it. The
mode is set to `0600` on the file descriptor **before** the rename — between a default-mode
create and a later `chmod` there is a window where `auth.json` and the SSH key are world
readable.

## Web layer — server-rendered, one operator

Jinja templates with HTMX for the live table. One person looking at a list of machines does not
need a client-side framework, and a build step plus a second container plus a CSP is a lot of
apparatus to add to a product whose entire shape is "one image, one volume". HTMX is vendored
rather than loaded from a CDN — the documented deployment is a private network that may have no
route to the internet, and an air-gapped rack should not get a broken page.

**Authentication is not optional, and "it is only on the LAN" is not an access-control story.**
This service holds an SSH key that reaches every managed machine and can write `sudoers` on
them; site-to-site links, a guest VLAN and a forwarded port all end at the same login form.
Single-user is a deliberate scope — there are no roles to divide when every capability is
administrative — and that removes user management, not authentication. bcrypt, a JWT in an
httpOnly `SameSite=Lax` cookie, a 12-character minimum, and a five-attempt lockout.

Four decisions worth keeping:

- **Until an account exists, every path redirects to `/setup`.** Otherwise the window between
  first boot and the operator finishing setup is a window in which the dashboard — the fleet's
  inventory — is served to whoever asks. `/health` and `/static/` are the exceptions: an
  orchestrator has to be able to tell "starting" from "wedged", and the setup page needs its own
  stylesheet.
- **`/setup` closes permanently once an account exists**, guarded both in the route and in
  `create_account()`. It is the one route reachable without a session; without the guard it is a
  password reset for anyone who can load the page.
- **The cookie is not `Secure`.** The documented deployment is a private network, often plain
  http, where a `Secure` cookie is silently dropped — presenting as "the login form works but I
  am never logged in". TLS belongs to a reverse proxy in front, not to this app.
- **A cookie whose username is no longer the operator's is rejected.** A restored volume can
  carry an old session into a fleet that now has a different account.

## Three states, not two

`asleep` and `down` are different conditions and the dashboard paints them differently. A
machine that is off *and is meant to be off* is not a fault; a status page that shows both in
red teaches its reader to ignore red. The distinction is derived, not configured — see the
`wol_mac` note above.

Probes run in a thread pool behind a 10-second TTL cache. A probe is a TCP connect whose
timeout is the *normal* case here, since most of the fleet is supposed to be asleep: serially, a
fleet with six sleeping machines costs `6 x timeout` before the page renders. Measured against a
real three-machine fleet with one sleeping host: **3.02s cold, 0.01s cached** — cold cost is one
timeout, not the sum of them. The cache also stops a left-open browser tab from generating
continuous traffic to every machine in the rack.

## Packaging — and the networking choice, measured

One Alpine image, **126 MB** on disk (`docker images` DISK USAGE, not `inspect .Size` — those
differ by several times). Dependencies account for 39 MB of it. Runs as uid 1000, non-root; the
UID is fixed by an ARG because `/data` is the installation and operators bind-mount it, so a UID
that drifts between releases turns an upgrade into "permission denied" on their own files.

`--only-binary=:all:` is carried over as a guard, not an optimisation: every dependency ships a
musllinux wheel today, and the day one stops, pip would silently fall back to a source build
needing a Rust and C toolchain the image does not have — an hour of compiling on a Pi, or a
confusing failure. With the flag the build fails immediately and names the package. **A failure
there is information; do not remove the flag to make a build pass.**

### Wake-on-LAN requires host networking

Measured, not assumed. A magic packet is a broadcast. Sent from a container on a bridge network
it **succeeds** — `sendto` returns, no error, no exception — and never reaches the LAN.
`tcpdump` on the host's LAN interface, same image, same call, same target:

| Network mode | `sendto` | Packets seen on the LAN interface |
|---|---|---|
| bridge | returned cleanly | **0** |
| host | returned cleanly | **1** — `192.0.2.6.47361 > 192.0.2.255.9: UDP, length 102` |

102 bytes is exactly `6 + 16 x 6`, a well-formed magic packet. The default compose file
therefore uses `network_mode: host`. Everything else works on a bridge; only waking does not,
and it fails in the way that is hardest to diagnose, so the default is the mode that works.

Waking from a bridge deployment needs a relay — an always-on host that sends the packet on
Timar's behalf, over SSH. Not implemented; it would also enable waking machines in other
subnets, which host networking cannot do.

> **⚠️ The healthcheck must read `TIMAR_PORT`.** The exec form of `HEALTHCHECK CMD` does not
> expand environment variables, so an inline `python -c` with a hardcoded 8080 left the
> container reporting `starting` **forever** for anyone who changed the port — which the compose
> file explicitly invites them to do. It is a module (`timar.web.healthcheck`) that reads the
> variable itself. Found by running the image, not by building it; `tests/test_web.py` now pins
> it so a build is not needed to catch a regression.

## Settings — validation, secrets, and proving a connection

Servers, log-sweep defaults, the model connection and Telegram are edited from the UI and
written back through `config.save()`. Saving rewrites `config.yaml`, which drops hand-written
comments — the page says so rather than surprising anyone.

**Validation lives in `validate.py`, not in the form handlers.** The same rules have to hold for
a config file written by hand, and a rule that only exists in a request handler does not. Errors
are collected and reported together: a form that reveals its next problem only after you fix the
current one is a form people learn to dread.

**Add and edit share one handler.** They share every rule, and splitting them is how the two
paths drift until one of them stops checking something.

**Secrets are never sent to the browser.** The forms report *whether* a key is stored, never
what it is, and an empty key field means "leave it as it was". That has to be the rule rather
than "empty means delete", because a blank box is the **normal** state of the field on every
visit — treating it as a deletion would wipe the credential on any unrelated edit to the same
form. Switching provider does not carry the old key across: a key for one vendor is meaningless
to another and quietly wrong to keep. Verified live: with a key and a bot token stored, the
rendered page contains neither, while the chat id — not a secret — round-trips.

**The session guard is declared on the router**, not per route, so a new settings endpoint is
protected for *being* a settings endpoint rather than because whoever added it remembered a
decorator. A test walks every route to prove it.

### Test buttons, because credentials fail at 3am otherwise

Both the model connection and Telegram have a test button. A credential exercised only by the
nightly job is one you discover is wrong on the morning the report did not arrive.

The failure text matters as much as the success. Verified against a live Ollama endpoint: a
good connection answers `Model replied: ok`; a wrong model name returns the upstream's own words
— `ollama returned HTTP 404: {"error":"model 'does-not-exist' not found"}` — HTML-escaped,
because that string is a third-party response body being written into the page.

Deleting a server also drops it from any hypervisor's `manages_vms`. A guest left in that list
after its own entry is gone is a guest nothing will ever start.

## Open / next

- SSH key generation, key push, and sudoers enrolment from the UI
- In-process scheduler with supervised tasks and a visible heartbeat
- WOL relay, for bridge deployments and cross-subnet wake
- Multi-arch build and publish (amd64 + arm64) to GHCR
