---
name: Bug report
about: Something is not working as expected
title: ""
labels: bug
assignees: ""
---

### What happened

A clear description of the bug.

### Steps to reproduce

1.
2.
3.

### Expected vs. actual

- **Expected:**
- **Actual:**

### Environment

- Timar version / commit:
- Deployment: <!-- host network or bridge? behind a reverse proxy? -->
- Affected machine's platform: <!-- linux / openwrt / proxmox -->
- Is that machine on-demand (has a MAC, or is a guest), or always on?
- What was running: <!-- log sweep / update run / wake / shutdown / enrolment / just the UI -->

### Logs

<!--
`docker logs timar`, and the report behind the run if there is one (Reports → the run).

⚠️ Redact before pasting. Timar's logs and reports contain your fleet: hostnames, IP and MAC
addresses, usernames, and the output of commands run on your machines.
-->

```
```

### If it involves waking a machine

<!-- Wake-on-LAN fails silently by design: the send succeeds and nothing happens. If you can,
say whether the packet reached the target's segment (`tcpdump -i <iface> port 9`), and whether
Timar is on a host or bridge network, with or without a relay. -->
