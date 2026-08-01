"""Validation for operator-supplied configuration.

Pure functions, deliberately separate from the web layer: the same rules have to hold for a
config file written by hand, and a rule that only lives in a form handler does not.

Errors are collected rather than raised one at a time — a form that reports its first problem,
then the next one after you fix it, is a form people learn to dread.
"""
from __future__ import annotations

import re

from .platforms import PLATFORMS

MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def server(form: dict, existing_names: set[str], original_name: str | None = None) -> dict:
    """Validate and normalise one server entry.

    `original_name` is the name being edited, so renaming a server to itself is not a clash.
    """
    errors: list[str] = []
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    user = (form.get("user") or "").strip()
    platform = (form.get("platform") or "").strip()

    if not name:
        errors.append("Name is required.")
    elif not NAME.match(name):
        # The name is used in report text, log lines and as a dictionary key; keeping it to
        # plain characters avoids a whole class of quoting surprises later.
        errors.append("Name may contain only letters, digits, dot, dash and underscore.")
    elif name != original_name and name in existing_names:
        errors.append(f"A server named {name!r} already exists.")

    if not host:
        errors.append("Address is required.")
    if not user:
        errors.append("SSH user is required.")
    if platform not in PLATFORMS:
        errors.append(f"Platform must be one of: {', '.join(PLATFORMS)}.")

    entry: dict = {"name": name, "host": host, "user": user, "platform": platform}

    mac = (form.get("wol_mac") or "").strip()
    if mac:
        if not MAC.match(mac):
            errors.append("Wake-on-LAN MAC must look like aa:bb:cc:dd:ee:ff.")
        else:
            entry["wol_mac"] = mac.lower().replace("-", ":")
        if broadcast := (form.get("wol_broadcast") or "").strip():
            entry["wol_broadcast"] = broadcast
    elif (form.get("wol_broadcast") or "").strip():
        # Silently keeping a broadcast address for a machine with no MAC would leave a setting
        # visible in the file that can never take effect.
        errors.append("A broadcast address needs a MAC address to go with it.")

    for optional in ("update_cmd", "context"):
        if value := (form.get(optional) or "").strip():
            entry[optional] = value

    if errors:
        raise ValidationError(errors)
    return entry


def log_check(form: dict) -> dict:
    errors: list[str] = []
    try:
        hours = int(form.get("journal_hours", 6))
        if not 1 <= hours <= 168:
            errors.append("Log window must be between 1 and 168 hours.")
    except (TypeError, ValueError):
        errors.append("Log window must be a whole number of hours.")
        hours = 6

    try:
        threshold = int(form.get("disk_threshold", 85))
        if not 50 <= threshold <= 99:
            # Below 50 every machine is a finding and the report becomes noise; at 100 a full
            # disk is reported only once it is too late to act on.
            errors.append("Disk threshold must be between 50 and 99 percent.")
    except (TypeError, ValueError):
        errors.append("Disk threshold must be a whole number.")
        threshold = 85

    if errors:
        raise ValidationError(errors)
    return {"journal_hours": hours, "disk_threshold": threshold}


def llm(form: dict, existing: dict | None) -> dict | None:
    """Validate the model connection. An empty provider clears it, which is a valid state."""
    from .llm import PROVIDERS

    provider = (form.get("provider") or "").strip()
    if not provider:
        return None

    errors: list[str] = []
    if provider not in PROVIDERS:
        errors.append(f"Provider must be one of: {', '.join(PROVIDERS)}.")

    entry: dict = {"provider": provider}
    if model := (form.get("model") or "").strip():
        entry["model"] = model
    if base_url := (form.get("base_url") or "").strip():
        entry["base_url"] = base_url

    # An empty key field means "leave it as it was", never "delete it". The form cannot show the
    # stored value — it is never sent to the browser — so a blank box is the normal state of the
    # field on every visit, and treating that as a deletion would wipe the key on any unrelated
    # edit. Clearing is done by changing the provider.
    key = (form.get("api_key") or "").strip()
    if key:
        entry["api_key"] = key
    elif existing and existing.get("provider") == provider and existing.get("api_key"):
        entry["api_key"] = existing["api_key"]

    if provider == "anthropic" and not entry.get("api_key"):
        errors.append("An API key is required for Anthropic.")

    if errors:
        raise ValidationError(errors)
    return entry


def telegram(form: dict, existing: dict | None) -> dict | None:
    chat_id = (form.get("chat_id") or "").strip()
    token = (form.get("token") or "").strip()
    if not token and existing:
        token = existing.get("token", "")  # same leave-blank-to-keep rule as the API key

    if not token and not chat_id:
        return None

    errors: list[str] = []
    if not token:
        errors.append("Bot token is required.")
    if not chat_id:
        errors.append("Chat ID is required.")
    if errors:
        raise ValidationError(errors)
    return {"token": token, "chat_id": chat_id}
