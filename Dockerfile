# Alpine rather than -slim: nothing in the dependency set needs glibc, and the Debian rootfs
# alone costs more than every Python package here combined.
FROM python:3.13-alpine

# A fixed UID because /data is the whole installation and operators bind-mount it. A UID that
# drifts between releases turns an upgrade into "permission denied" on the operator's own files.
ARG UID=1000
ARG GID=1000

RUN addgroup -g "$GID" timar \
 && adduser -D -u "$UID" -G timar -h /home/timar timar

WORKDIR /app
COPY pyproject.toml README.md ./
COPY timar/ ./timar/

# --only-binary=:all: is a guard, not an optimisation. Every dependency currently ships a
# musllinux wheel; the day one stops, pip would silently fall back to a source build needing a
# Rust and C toolchain this image does not carry -- an hour of compiling on a Raspberry Pi, or a
# confusing failure. With the flag the build fails immediately and names the package.
# A failure here is information. Do not remove the flag to make a build pass.
RUN pip install --no-cache-dir --only-binary=:all: . \
 && pip uninstall -y pip setuptools wheel \
 && find /usr/local -name '__pycache__' -type d -prune -exec rm -rf {} +

# Created here so a *named* volume inherits the right ownership when Docker initialises it.
# A bind mount does not: the host directory's ownership wins, so `chown 1000:1000` it yourself.
RUN mkdir -p /data/ssh && chown -R timar:timar /data

USER timar
VOLUME ["/data"]
EXPOSE 8080

ENV TIMAR_DATA=/data \
    TIMAR_HOST=0.0.0.0 \
    TIMAR_PORT=8080 \
    PYTHONUNBUFFERED=1

# Reports liveness without revealing anything about the fleet, and distinguishes "starting" from
# "wedged" -- which matters most on first boot, before any account exists.
#
# A module, not an inline `python -c`: the exec form below does not expand environment
# variables, so a hardcoded port left the container reporting `starting` forever for anyone who
# changed TIMAR_PORT. Caught by running it, not by building it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-m", "timar.web.healthcheck"]

CMD ["python", "-m", "timar.web"]
