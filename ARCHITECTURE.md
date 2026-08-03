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

**On-demand is derived, not flagged** — a machine with a `wol_mac` is by definition one expected
to sleep, so its being offline is stated as normal rather than reported as an outage. A guest
started by its hypervisor has no `wol_mac` of its own — it cannot, it is started by `qm` — so it
**inherits** the answer from its host through `manages_vms`.

Inherited rather than granted to every guest, because the two mistakes are not symmetric.
Calling an on-demand guest always-on produces a nightly false alarm, which is noise; calling a
guest of an *always-on* host on-demand normalises its outage, so a 24/7 VM that has actually
crashed is described as sleeping soundly. Noise is recoverable, silence is not.

The rule lives in exactly one function, `config.on_demand`, because it did not used to. It was
written out once per consumer — dashboard, analysis prompt, settings table — and the third copy
dropped the guest clause, so the same Kali VM was *asleep* on one page and *always on* on
another. The fleet's own description must not depend on which page you are looking at.

## The `/data` volume is the installation

```
/data/
  config.yaml     servers, schedules, LLM and notifier settings
  auth.json       the operator's username and password hash
  secret_key      signs session cookies
  state.json      last run results, heartbeats
  reports/        one JSON file per finished run — the archive behind /reports
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

## The report archive — a series, not a snapshot

`state.json` keeps the latest report per job, and for the dashboard that is the right answer:
it is read on every poll, and a file that grows without bound would make the cheapest read in
the product the most expensive one. But a snapshot cannot answer the question an operator
actually asks a week later — **when did this start?** A disk that crossed 90% last Tuesday, a
host that has been unreachable for three sweeps, an update that has failed every Friday for a
month: none of those are visible in one run, only in a series.

Telegram *is* a series, which is why the answer used to be "scroll up in the chat". That copy
is outside the tool, not searchable by job, gone when the chat is cleared — and an installation
with no Telegram configured never had it at all. So the archive is kept in `/data/reports/`, and
the notification becomes what it should always have been: a copy, not the record.

**One JSON file per run, not rows appended to `state.json`.** Writing a report then cannot
corrupt or lose the job state the scheduler depends on; pruning is `unlink` rather than a
read-modify-write of a growing file; and the filename carries the timestamp and the job, so
listing and filtering never open a file.

Four things that are easy to get wrong here, each held by a test:

- **Retention is per job, not overall.** A daily sweep and a weekly update share the archive.
  One global cap of N silently evicts every update run to make room for sweeps — precisely
  backwards, since the rarer report is the more valuable one.

- **The filename is the sort key, so it needs sub-second precision.** Seconds alone are not
  enough: a job that fails immediately can finish twice inside one, and a `-2` disambiguating
  suffix sorts *before* the entry it followed — reversing the two, and pruning the wrong one
  first.

- **A failed run is archived too.** The failing Friday update is the entry most worth finding
  three weeks later; an archive of only the clean runs describes a fleet that never breaks.

- **Archiving never fails a run.** The work happened, the outcome is already in `state.json`
  and already sent to Telegram. Reporting a successful sweep as failed because its copy could
  not be written trades something that matters for something that does not.

The id comes back in a URL, so `get()` matches it against a pattern rather than trusting it —
`../auth.json` would otherwise read the password hash out of the volume and render it on the
page.

## Web layer — server-rendered, one operator

Jinja templates with HTMX for the live table. One person looking at a list of machines does not
need a client-side framework, and a build step plus a second container plus a CSP is a lot of
apparatus to add to a product whose entire shape is "one image, one volume". HTMX is vendored
rather than loaded from a CDN — the documented deployment is a private network that may have no
route to the internet, and an air-gapped rack should not get a broken page.

The panels that poll — fleet, jobs — have fragment endpoints, because sending a whole page every
ten seconds to redraw one table is waste that repeats forever. The report filter does not: it
re-requests `/reports` and HTMX takes the list out of the response with `hx-select`. A one-off
click can afford the page, and it buys a correct URL — `hx-push-url` on a fragment endpoint puts
`/fragments/...` in the address bar, which renders a bare `<table>` on the next refresh. The
filter is also a plain `GET` form underneath, so it works with no scripting at all.

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

## Power from the row that reports the state

The dashboard's last column is the action that fits the state: `wake up` for a machine that is
asleep, `shutdown` for one that is up, `n/a` for a machine that is always on. Noticing a machine
is asleep and having to go somewhere else to wake it is the trip an operator makes all day, and
the state and the button belong to the same row.

Three decisions hold this together:

- **A guest is not woken with a magic packet.** It has no wake address and never can have one —
  it is started by `qm` on its hypervisor — so `power.wake` dispatches on whether anything in
  the fleet `manages_vms` this machine, not on whether it has a `wol_mac`. Keying off the MAC
  would give every VM a button that only ever answers *no MAC address configured*. The same
  split applies downwards: a guest is stopped with `qm shutdown`, a host over its own SSH
  connection with the platform's command.
- **An always-on machine is offered no way down.** Shutting one off over its own SSH connection
  works perfectly and leaves nothing to bring it back. The refusal lives in `power.shutdown`,
  not only in the template, because the button not being drawn is not the same as the route
  refusing — and it is the route that a bookmark, a script or a second browser tab reaches.
- **`qm shutdown` is bounded and never `--forceStop`.** A guest that ignores ACPI needs its
  operator, not the equivalent of its power cord pulled. The timeout expiring turns into a
  non-zero exit, which becomes the sentence on the page.

The failure that took the most care is the one that looks like success: **a connection dropping
after the shutdown command was accepted is the machine going down**, while a connection that
never opened means nothing was asked to stop. Both surface as an exception from paramiko, so
`power.shutdown` tracks whether the session was ever established — without that flag, an
unreachable host reports *is shutting down* and leaves an operator certain of the opposite. A
non-zero exit is reported rather than swallowed for the same reason: an account without
passwordless sudo cannot halt its own machine, and the refusal is otherwise silent.

The result is written to `#power-result`, which lives on the dashboard **outside** the polled
fragment. Inside it, the ten-second refresh would erase a failure's explanation before it could
be read. The actions themselves run in a thread, like every other blocking call here — a `qm
shutdown` on the event loop would freeze the scheduler and every other tab, including the
polling that is the operator's only evidence the thing worked.

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

### The relay

A magic packet is a broadcast, and a broadcast only reaches its own segment. That one fact
causes two problems, and a relay answers both: send the packet from a machine already on the
target's segment and already reachable over SSH. Timar manages such machines by definition.

- **Bridge deployments.** Measured from one bridge container, same image, same button:
  direct wake reported success and put **0 packets** on the LAN; a relayed wake put **1 packet**,
  `192.0.2.6.35205 > 192.0.2.255.9: UDP, length 102`, originating from the relay.
- **Other subnets** — a second site over a tunnel, an office network. Host networking cannot
  reach these at all, because the packet has to originate over there. This is the case the
  relay adds that no networking mode can.

The relay runs `python3` if present, `wakeonlan` otherwise, and says which it wanted if it has
neither. `etherwake` is deliberately not attempted: it needs an interface name and root, and
guessing the interface on someone else's machine is how you send the packet out of the wrong one
and report success.

> **⚠️ Two nested languages, one escaping tool each.** The relay runs Python inside a shell
> command. The first version escaped the Python-level values with `shlex.quote`, which returns a
> **bare word** for anything without shell metacharacters — so a hex MAC arrived in the Python
> source as an undefined identifier and the remote raised `NameError`. Nothing local could have
> noticed; the packet simply never went. Python-level values now use `repr`, the shell layer
> still uses `shlex.quote`, and a test **compiles** the generated script and executes it against
> a fake socket to check the bytes it would put on the wire.

Waking is also the one operation with no feedback of its own — the packet is fire and forget. A
machine that stays dark could mean a wrong MAC, a packet that never left the network, or
Wake-on-LAN disabled in firmware. The manual button exists so an operator can press it and watch
the dashboard, which is how those get told apart.

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

## The scheduler, and why supervision is the feature

Timar's predecessor lost its weekly update job during a host migration. The trigger silently
disappeared, the service went on reporting itself as running, and nobody noticed for two weeks.
Nothing was broken in any way anything could see — which is the whole point. **A job that stops
being scheduled produces no error, no output and no signal of any kind.**

The scheduler runs in the web process's event loop. One process means one PID, one log stream,
and `restart: unless-stopped` meaning what it says — but a single asyncio process has a failure
mode that had to be designed against rather than discovered:

> **A task that raises does not stop the process. It disappears.** The server keeps serving
> pages, the container stays `healthy`, and the schedule never fires again.

Which is a different mechanism from the original incident with exactly the same shape. So:

1. **Every long-lived task runs under a supervisor** that catches the exception, logs the full
   traceback — the trace that would otherwise vanish with the task — and restarts after a pause.
   A crash costs one cycle, not the schedule. `CancelledError` is re-raised, never retried:
   shutdown is not a failure.
2. **Every cycle writes a heartbeat**, and the dashboard shows *last run* and *next run* per job.
   A frozen timestamp on a healthy-looking container is the only outward sign that a loop has
   stopped, so it is on the first screen rather than in a log.
3. **A failing job is recorded, not raised.** Letting it propagate would kill the loop that
   called it — precisely the failure being defended against.
4. **An unparseable schedule keeps the loop alive.** An operator typo is an operator error; the
   loop has to survive it so the UI can be used to fix it.

**Jobs run in a worker thread, never on the event loop.** paramiko is synchronous and an update
run can take ten minutes; executed inline it would freeze the UI for the duration, and a
dashboard going dead while an update runs is the opposite of what an operator needs then. One
lock per job stops a manual run landing on top of a scheduled one.

**The loop sleeps in one-minute steps** rather than straight through to the next run: a
multi-day sleep would freeze the heartbeat and ignore a schedule edited in the meantime.

### Schedules are a vocabulary, not cron

`daily at HH:MM`, `weekly on <day> at HH:MM`, `every N hours`. A cron expression is a thing
operators mistype in ways that are invisible until the run does not happen; a dropdown and a
time field cannot express `0 7 * * 5` wrongly. Times are the container's local time — set `TZ`,
or a report scheduled for 07:00 arrives at 04:00.

Two arithmetic details, each pinned by a test:

- **Interval schedules count from the last run, not from startup.** Otherwise a restart resets
  the clock, and a six-hourly job on a host that restarts hourly never fires at all — silently.
- **A run missed while the process was down happens at the next opportunity**, rather than being
  quietly counted as already done.

A schedule is validated *as if enabled* when saved, because `next_run` short-circuits on a
disabled one — otherwise a bad time could be stored now and only break whenever someone enables
it.

### Run now

A scheduler you cannot trigger is a scheduler you cannot verify; the operator has to be able to
prove the plumbing works without waiting a week for the next window. The route starts the job
and returns the panel immediately — waiting for an update run would hold the request open for
ten minutes and time out at every proxy in between. Triggering an update asks for confirmation
first, because it wakes machines and installs packages.

**Delivery failure never fails the job.** The sweep ran and the findings are in the UI;
conflating "could not reach Telegram" with "the update broke" sends the operator to the wrong
problem.

### How long a host gets, and what happens when it runs out

`update_timeout` per server, defaulting to 1800 seconds. The first value was 300, picked before
anything real had run against it, and it was wrong in a way worth writing down.

Two ordinary things pass five minutes on their own: a kernel upgrade that rebuilds a DKMS
module, and an update command that also pulls container images — one 7 GB image is enough. So
the hosts most worth updating were the ones most likely to trip it.

The failure is worse than being early, because the timeout does not reach the far end. `run`
gives it to paramiko as a *channel read* timeout, so nothing is signalled to the remote: the
upgrade carries on while Timar records a failure it invented, and the next run arrives to find a
dpkg lock held by the command it thinks it already lost. Two consequences follow, both
deliberate:

- **The remote command is not killed.** Wrapping an operator's command in `timeout` would mean
  re-quoting a shell string that already contains `&&` chains and nested quotes — the same class
  of bug the WOL relay hit, where a value quoted for the wrong layer arrived as an undefined
  identifier. A late report is cheaper than a mangled command.
- **The host is not shut down.** The early return skips the shutdown that normally puts an
  on-demand machine back as it was found. Powering a machine off while apt is still writing is
  the one outcome worse than leaving it on.

The cost of a generous default is that `run_updates` walks the fleet in sequence, so a wedged
host delays the ones behind it by exactly this much. That is why the value is per server and
bounded, rather than one number for everybody.

### What a failed update is allowed to say

Both streams, tails of each, labelled. This started as `(stderr or stdout)[-500:]`, which reads
as a sensible preference and is not one.

The first real fleet run proved it. A host's update command was a wrapper script that updates
every Compose stack on the machine; one service failed to pull, so the script had already stopped
it, could not bring it back, and exited non-zero after printing the line that mattered — *"these
services failed: speedtest"* — on **stdout**. But `docker compose` writes its progress to
**stderr**, so stderr was not empty, so stderr won: the report carried 500 characters of layer
IDs and `Pull complete`, ending mid-word, naming nothing. Which service had been left down was
worked out from a container missing in `docker ps -a`.

The lesson is not "prefer stdout" — that buries an ordinary error message just as completely.
It is that a command has two outputs and a report that shows one of them is guessing which one
the operator needed. Each is tailed separately, because the noisy stream still earns its last few
lines, and a command that fails while printing nothing at all says so in words rather than
leaving a red mark that reads like a bug in Timar.

The report layer does not re-truncate: bounding the text twice, once at each end, is how the
naming line was lost in the first place.

## Enrolment — the most dangerous surface in the product

Installing Timar's key on a host, and optionally granting it passwordless sudo. One keypair per
installation, Ed25519, generated in-process with `cryptography` (the image carries no OpenSSH
client) into `/data` on first use — regenerating on start would mean re-enrolling every host
after every restart, which is the kind of friction that gets solved by turning key checking off.

**The password.** Enrolment is the one operation that takes the operator's SSH password. It is
held for the length of one request, written only to the SSH channel, and never persisted, never
logged, and never echoed back to the page — including on the error path, where re-rendering the
form with the field refilled would put it in browser history and every proxy in between. For
sudo it goes to `sudo -S` on **stdin**, never the command line, where `ps` on the target would
show it to every other user on that machine.

**The sudoers file is validated before it is installed.** A malformed file in `/etc/sudoers.d/`
does not degrade — it breaks `sudo` for everyone on that machine, including the session you
would repair it from. The candidate is written to a temp file, checked with `visudo -cqf`, and
only then moved into place with `install -m 0440 -o root -g root`. A rejected file is discarded
and `/etc/sudoers.d/` is untouched. **That step is not optional and must never be optimised
away**; a test asserts the validation precedes the install rather than merely sitting near it.

Sudo is not offered where it cannot work — OpenWrt has none, and a root account already has it.
Offering a button that cannot work is worse than not offering one.

**Host keys are pinned on first sight** (`/data/ssh/known_hosts`), for enrolment *and* for every
routine connection. This replaces a bare `AutoAddPolicy` with no known-hosts file, which
accepted any key from any host on every connection and wrote nothing down — not weaker
protection than TOFU, but none, and indistinguishable from the outside.

### The bug this feature found in itself

The first version used `~/.ssh/authorized_keys` as the platform path. `shlex.quote` wraps a
value in single quotes, and **a tilde inside quotes is not expanded by the shell** — so the
command created a literal directory named `~`, wrote an authorized_keys file into it that
nothing would ever read, exited 0, and reported success. Exactly the silent failure the comment
above that constant warns about.

Nothing caught it except connecting afterwards with the key. That is why `verify()` exists and
why the enrolment result reports it: **the password connection succeeding says nothing about
whether the key will be accepted**, and the key is what every later run depends on. Paths are
now home-relative or absolute, rendered as `"$HOME"/...`, with a test asserting no platform
path contains a tilde.

Verified end to end against a throwaway sshd container: wrong password refused, key installed at
`/home/deploy/.ssh/authorized_keys` mode 600, **one line after three enrolments** (idempotent),
`/etc/sudoers.d/timar` mode 440 root:root with `visudo -c` reporting *parsed OK*, the host key
recorded in `known_hosts`, and a key-only connection confirming `passwordless sudo works`.

## Open / next
- In-process scheduler with supervised tasks and a visible heartbeat
- WOL relay, for bridge deployments and cross-subnet wake
- Multi-arch build and publish (amd64 + arm64) to GHCR
