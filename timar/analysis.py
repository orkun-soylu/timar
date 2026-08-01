"""Turn a log sweep into a short written assessment.

The whole point of this module is that **it knows nothing about any particular fleet**. Its
predecessor carried a hardcoded list of one operator's machines — names, IP addresses, GPU
models — inside the system prompt string. That made the prompt unpublishable, and it meant
adding a server required a code change.

Everything the model is told about the fleet is derived here from configuration: the server
list, which machines are expected to be asleep, whatever the operator wrote in `notes.md`, and
each server's own `context` line.
"""
import logging

from .llm import LLMConfig, LLMError, complete

logger = logging.getLogger(__name__)

SYSTEM = """\
You are the log analyst for a small, self-hosted server fleet. You are given the results of an \
automated sweep and write a short assessment for the operator.

Be concrete and brief: three to five sentences. Lead with anything that needs action today. \
Name the server and the specific evidence for each point. If everything is healthy, say so \
plainly in one sentence rather than padding.

Do not speculate about causes you have no evidence for, and do not recommend action you cannot \
justify from the findings. Say when something is inconclusive.\
"""


def _fleet_facts(cfg: dict) -> str:
    """What the model needs to know about *this* fleet, entirely from config.

    The on-demand distinction is the one that matters most: a machine that exists to be woken
    for a job is *supposed* to be off, and reporting it as an outage every single night is how
    an operator learns to ignore the report. It is derived from `wol_mac` rather than a separate
    flag — a machine with a wake address is by definition one that is expected to sleep.
    """
    servers = cfg.get("servers", [])
    if not servers:
        return ""

    managed_guests = {
        guest["server_name"]
        for host in servers
        for guest in host.get("manages_vms", [])
    }

    lines = ["The fleet:"]
    for server in servers:
        bits = [server.get("platform", "linux")]
        if server.get("wol_mac") or server["name"] in managed_guests:
            bits.append("on-demand — being offline is normal and is not a fault")
        line = f"- {server['name']} ({', '.join(bits)})"
        if note := server.get("context"):
            line += f": {note}"
        lines.append(line)
    return "\n".join(lines)


def build_system_prompt(cfg: dict, notes: str = "") -> str:
    sections = [SYSTEM]
    if facts := _fleet_facts(cfg):
        sections.append(facts)
    if notes.strip():
        # Operator-authored standing context: known-benign warnings, local conventions, an
        # explanation of why one machine always looks odd. Kept last so it can qualify anything
        # above it.
        sections.append(f"Operator notes:\n{notes.strip()}")
    return "\n\n".join(sections)


def format_findings(results) -> str:
    """The sweep, as the model sees it. Plain text, one block per server."""
    lines = []
    for r in results:
        if r.offline:
            lines.append(f"{r.server}: offline, not checked")
            continue
        if not r.success:
            lines.append(f"{r.server}: could not be reached over SSH — {r.error}")
            continue
        if not r.has_issues:
            lines.append(f"{r.server}: clean")
            continue

        lines.append(f"{r.server}:")
        if r.journal_errors:
            lines.append(f"  system log: {len(r.journal_errors)} error lines, first few:")
            lines.extend(f"    {line[:200]}" for line in r.journal_errors[:5])
        for issue in r.disk_issues:
            lines.append(f"  disk: {issue}")
        if r.containers_stopped:
            lines.append(f"  stopped containers: {', '.join(r.containers_stopped)}")
        for path, job in r.job_logs.items():
            if not job.ran_today:
                lines.append(f"  job {path}: did not run today (last run: {job.last_run or 'unknown'})")
            elif not job.completed:
                lines.append(f"  job {path}: started at {job.last_run} but did not complete")
            for err in job.errors:
                lines.append(f"    {err[:150]}")
        for path, hits in r.log_file_issues.items():
            lines.append(f"  log {path}: {len(hits)} matching lines, first few:")
            lines.extend(f"    {line[:200]}" for line in hits[:3])

    return "\n".join(lines)


def analyze(llm: LLMConfig | None, cfg: dict, results, notes: str = "") -> str | None:
    """Written assessment of a sweep, or None when no model is configured or reachable.

    None rather than an exception: the sweep's own findings are the product, and the analysis is
    commentary on top. A model that is down, out of credit or misconfigured must not take the
    report with it.
    """
    if llm is None:
        return None
    if not results:
        return None

    try:
        return complete(
            llm,
            build_system_prompt(cfg, notes),
            f"Results of the latest sweep:\n\n{format_findings(results)}",
        )
    except LLMError as e:
        logger.error("log analysis unavailable: %s", e)
        return None
