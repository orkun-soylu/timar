---
name: Feature request
about: Suggest an idea or improvement
title: ""
labels: enhancement
assignees: ""
---

### Problem

What are you trying to do that Timar does not support today?

### Proposed solution

What you would like to happen.

### Scope / alternatives

Anything you have considered, and how big you think the change is.

<!--
Two things worth checking first, because they are deliberate rather than missing:

- Several omissions are decisions with reasons written down in `ARCHITECTURE.md` — no agent on
  the managed machines, no cron-expression schedules, no default update command on OpenWrt.
- Timar is built for a fleet that is mostly *off*. A proposal that assumes a machine can report
  its own state has to say what happens while that machine is asleep.
-->

### If it is a new platform

<!-- Adding one means a `Platform` subclass with commands that exist on it: system log, disk,
containers, shutdown, and where sshd reads authorised keys from. Every command in that file was
run against a real host of that platform first — the wrong command usually fails quietly rather
than loudly. Say which platform, and whether you have one to test against. -->
