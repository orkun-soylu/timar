# Contributing

Thanks for considering a contribution. This document covers the **legal requirement** (DCO
sign-off) and the **working habits** of this project — particularly how it expects a change to
be verified, which is stricter than "the tests pass".

---

## 1. Sign your commits (required)

This project uses the **Developer Certificate of Origin** instead of a CLA. It is the
lightweight way of stating that you have the right to submit the code under this project's
license. There is no separate document to sign — you add one line to your commit.

```bash
git commit -s -m "fix: ..."
```

`-s` appends:

```
Signed-off-by: Your Name <you@example.com>
```

The name and email must be real (a pseudonym is fine, but it has to be a reachable identity).
Full text: [`DCO`](DCO).

If you forgot to sign off, amend the last commit:

```bash
git commit --amend -s --no-edit && git push --force-with-lease
```

For several commits at once: `git rebase --signoff main`

---

## 2. Development environment

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

To run the app against a throwaway data directory rather than your real one:

```bash
TIMAR_DATA=/tmp/timar-dev TIMAR_PORT=8099 .venv/bin/python -m timar.web
```

`TIMAR_DATA` is the whole installation — config, credentials, state, reports and the SSH key.
Point it somewhere disposable while developing and you cannot damage a live fleet.

The container is what ships:

```bash
docker build -t timar:dev . && docker compose up -d
```

---

## 3. Verification — the part that is not optional

**Timar's failures are quiet ones.** A magic packet that never leaves the host, a disk check
that exits non-zero and reports all-clear, a scheduled job that stops being scheduled — none of
these produce an error anywhere. That shapes how changes are expected to be verified.

### Run it, do not only test it

For anything that touches a machine, a network or a page, prove it against the real thing and
put the measurement in the pull request. Examples from this repository's own history:

- Wake-on-LAN from a bridge network: `tcpdump` on the LAN interface, **0 packets** — the send
  had reported success.
- The container healthcheck: found by *running* the image, not by building it.
- A CSS alignment fix: heading and cell x-coordinates read out of a headless browser rather
  than eyeballed.

A number in the pull request is worth more than an assurance.

### Mutation-test your guards

If you add a guard — a validation, an authorization check, a refusal — **deliberately break it,
watch the relevant test fail, and put it back.** A test that passes with the guard removed is
not testing the guard. Check that your mutation actually reached the file before concluding
anything.

### Write the failure into the test

Tests here are named after the behaviour they protect and carry a docstring saying *why* it
matters, usually naming the bug that motivated it. `test_a_connection_that_never_opened_is_a_failure`
is more useful to the next person than `test_shutdown_error`.

---

## 4. Code and commit conventions

**Code:** match the surrounding file — its comment density, naming and idioms. Comments explain
**why**, not what. If a decision looks counter-intuitive, write down the reason and, where one
exists, the measurement behind it. Comments, docstrings and test names are in English.

**Platform commands** (`timar/platforms.py`) must be run against a real host of that platform
before being written down. busybox and coreutils disagree in ways that fail *quietly*: a `df`
flag that GNU accepts and busybox rejects produced a disk check that silently passed on every
router it was pointed at.

**Commit subjects:** `type: short description`, with a body explaining the why. Types used
here: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `style`, `revert`. If you are fixing a
bug, describe the symptom, the root cause and how you verified the fix — this project treats
its history as a diagnostic resource.

**Architectural decisions belong in [`ARCHITECTURE.md`](ARCHITECTURE.md).** If you made a
lasting design decision, or discovered a pitfall someone else will hit again, write it down
there. It is the file that explains why the code is shaped the way it is, and it is expected to
grow with the code rather than after it.

---

## 5. Before opening a pull request

- [ ] Commits signed off (`git commit -s`)
- [ ] Tests pass (`.venv/bin/python -m pytest`)
- [ ] If you added a guard, you ran a **mutation test**
- [ ] Behavioural changes verified by running them, with the evidence in the PR
- [ ] `ARCHITECTURE.md` updated if behaviour or a design decision changed
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if the change is user-visible
- [ ] No secrets and no real infrastructure: no tokens, keys, MAC addresses, hostnames or IPs
      from a live fleet — test data must not be lifted from a real network

---

## 6. Security

If you find a vulnerability, **do not open a public issue** — contact the maintainer directly.

Things to know while contributing:

- **Timar holds an SSH key that reaches every machine it manages** and can write `sudoers` on
  them. Its login page is the only thing in front of a fleet. Anything that weakens
  authentication, or that renders a value into the page without escaping it, is a bigger
  problem here than the same bug in an ordinary web app.
- **The enrolment password lives for one request.** It goes to the SSH channel and nowhere
  else — never persisted, never logged, never echoed back to the page, and to `sudo -S` on
  stdin rather than the command line where `ps` would expose it.
- **The sudoers file is validated with `visudo` before it is installed.** A malformed file in
  `/etc/sudoers.d/` breaks `sudo` for everyone on that machine, including the session you would
  repair it from. That step must never be optimised away.
- **Host keys are pinned on first sight.** Do not replace the known-hosts policy with a bare
  `AutoAddPolicy` that writes nothing down; that is not weaker protection than TOFU, it is
  none.
- Remote output — log lines, SSH errors, provider responses — is **untrusted text**. It is
  escaped before it reaches the page and must stay that way.
