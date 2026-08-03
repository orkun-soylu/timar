# Third-Party Notices

Timar itself is MIT licensed (see [`LICENSE`](LICENSE)). It redistributes the third-party asset
below, which carries its own license.

## htmx

Redistributed as [`timar/web/static/htmx.min.js`](timar/web/static/htmx.min.js), version
**2.0.4**, unmodified.

- **htmx** (https://htmx.org, https://github.com/bigskysoftware/htmx) — licensed under the
  **Zero-Clause BSD** license (0BSD):

  > Permission to use, copy, modify, and/or distribute this software for any purpose with or
  > without fee is hereby granted.
  >
  > THE SOFTWARE IS PROVIDED “AS IS” AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO
  > THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT
  > SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR
  > ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
  > CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE
  > OR PERFORMANCE OF THIS SOFTWARE.

It is vendored rather than loaded from a CDN because the documented deployment is a private
network that may have no route to the internet at all, and an air-gapped rack should not get a
broken page.

## Runtime dependencies

Not vendored into this repository — they are installed from PyPI when the image is built, and
[`pyproject.toml`](pyproject.toml) is the authoritative list. They are reproduced here because
a published container image **does** redistribute them.

| Package | License |
|---|---|
| paramiko | **LGPL-2.1** |
| cryptography <em>(via paramiko)</em> | Apache-2.0 OR BSD-3-Clause |
| fastapi, PyYAML, PyJWT, pydantic | MIT |
| starlette, uvicorn, httpx, Jinja2 | BSD-3-Clause |
| bcrypt, python-multipart | Apache-2.0 |

**paramiko is the one copyleft dependency.** Timar imports it as an ordinary Python library and
does not modify it. It stays a separate, replaceable package in `site-packages` — including
inside the image, where `pip install paramiko==<other version>` swaps it — which is what
LGPL-2.1 §6 asks of a combined work. Do not vendor a patched copy into this repository without
reading that section first.

The base image is `python:*-alpine`; its contents are covered by the licenses of the packages
it ships.
