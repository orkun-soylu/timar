# Security policy

## Supported versions

Timar is at `0.x` and under active development. Security fixes land on `main` and ship in the
next release; older releases do not get backports.

| Version | Supported |
|---|---|
| `main` | ✅ |
| Latest release (`ghcr.io/orkun-soylu/timar:latest`) | ✅ until the next release |
| Anything older | ❌ |

If you pin a version, move to the release that carries the fix.

## Reporting a vulnerability

**Please do not report a vulnerability in a public issue, discussion or pull request.**

Report it privately through GitHub:
**[Report a vulnerability](https://github.com/orkun-soylu/timar/security/advisories/new)**
(the repository's *Security* tab → *Advisories* → *Report a vulnerability*). Only the maintainer
sees the report.

A useful report includes:

- the affected version or commit;
- how Timar is deployed — host or bridge networking, and anything in front of it (reverse proxy,
  TLS, VPN);
- steps to reproduce, or a proof of concept;
- the impact: what an attacker gains, and what access they need first (none, network access to
  the web port, the operator's session, a managed machine);
- logs if they help — with keys, tokens, hostnames and IP addresses removed.

## What to expect

Timar has a single maintainer, so this is best effort rather than a guaranteed timeline. You can
expect:

- a reply in the private advisory thread, where the report is discussed;
- if the issue is confirmed, a fix on `main`, a release, and a published GitHub security advisory
  that credits you, unless you prefer not to be named;
- coordinated disclosure: please keep the details private until the fix is released.

## Scope

Timar holds an SSH key that reaches every machine it manages and can grant itself `sudo` on them.
Its login page is the only thing in front of that key, so the bar is the fleet, not the web app.

**In scope** — anything that breaks a boundary Timar claims to enforce:

- authentication: reaching any page or action without the operator's session; creating or
  replacing the operator account once it exists; getting around the login lockout; forging the
  session cookie;
- cross-site request forgery or script execution in the UI — including through remote output
  (log lines, SSH errors, model or Telegram responses, container names), which Timar treats as
  untrusted text;
- secrets leaving where they belong: the SSH private key, the enrolment password (it must never
  be stored, logged, echoed to the page or put on a command line), or a stored model key or bot
  token reaching the browser;
- enrolment: a sudoers file installed without passing `visudo`, or a grant wider than the one
  Timar documents;
- SSH trust: a managed host whose pinned key has changed being accepted anyway;
- a managed machine, by what it returns over SSH, running code on the Timar host or on another
  managed machine;
- an open redirect, or request input reaching a `Location` or `Set-Cookie` header;
- a vulnerable dependency, when you can show the vulnerable code is reachable in Timar.

**Out of scope:**

- anything the **operator** can already do — the operator is trusted by design. Update commands,
  wake relays, link schemes and `config.yaml` run or say exactly what the operator wrote;
- someone who already has the `timar-data` volume or root on the Timar host — the volume holds
  the SSH key and the session signing key by design;
- the **first** connection to a host being trusted (trust on first use) — that is the documented
  model; a changed key afterwards is in scope;
- an instance exposed to the internet, or reachable before its operator account was created —
  the README says not to do the first, and the setup page is open to whoever reaches it first;
- log text sent to the model provider the operator configured for the log sweep;
- scanner output without a demonstrated impact, or a missing hardening header on its own;
- vulnerabilities in the machines Timar manages, or in the services it talks to (Docker, the
  package managers, model providers, Telegram) — please report those to their maintainers.
