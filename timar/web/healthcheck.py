"""Container health probe: `python -m timar.web.healthcheck`.

A module rather than an inline `python -c` in the Dockerfile, because the probe has to read
`TIMAR_PORT` — and the exec form of `HEALTHCHECK CMD` does not expand environment variables.
Hardcoding 8080 there made the container report `starting` forever for anyone who changed the
port, which is a change the compose file explicitly invites.
"""
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    port = os.environ.get("TIMAR_PORT", "8080")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError) as e:
        print(f"health probe failed on port {port}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
