"""Entry point: `timar` (installed) or `python -m timar.web`.

Binds to localhost by default. The container publishes the port itself, so the safe default
here is the one that does not expose an unconfigured installation to the network the moment it
starts — first boot is the window where no account exists yet.
"""
import os

import uvicorn

from .. import config


def main() -> None:
    config.ensure_dir()
    uvicorn.run(
        "timar.web.app:app",
        host=os.environ.get("TIMAR_HOST", "127.0.0.1"),
        port=int(os.environ.get("TIMAR_PORT", "8080")),
        log_level=os.environ.get("TIMAR_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
