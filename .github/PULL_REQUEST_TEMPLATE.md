<!--
Thanks for contributing to Timar. Please describe the change and tick the checklist.
See CONTRIBUTING.md for the full conventions.
-->

### What and why

<!-- What does this change do, and why? If it fixes a bug, describe the symptom, the root cause,
and how you verified the fix — this project treats its history as a diagnostic resource. -->

Closes #

### How it was verified

<!-- Timar's failures are quiet: a packet that never leaves the host, a check that exits
non-zero and reports all-clear, a job that stops being scheduled. For anything touching a
machine, a network or a page, say what you ran and what you measured. A number here is worth
more than an assurance. -->

### Checklist

- [ ] Commits are signed off (`git commit -s`)
- [ ] Tests pass (`.venv/bin/python -m pytest`)
- [ ] If I added a guard, I ran a **mutation test** (broke it deliberately, confirmed a test fails)
- [ ] Any new platform command was run against a real host of that platform
- [ ] `ARCHITECTURE.md` updated if behaviour or a design decision changed
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if the change is user-visible
- [ ] No secrets and no real infrastructure: no tokens, keys, MACs, hostnames or IPs from a live fleet
