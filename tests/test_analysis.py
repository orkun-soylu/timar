"""The system prompt must be built from configuration, never carry a baked-in fleet."""
import timar.analysis as analysis
from timar.analysis import analyze, build_system_prompt, format_findings
from timar.llm import LLMConfig, LLMError
from timar.log_checker import JobLogResult, LogResult

CONFIG = {
    "servers": [
        {"name": "web-01", "platform": "linux"},
        {"name": "gpu-01", "platform": "linux", "wol_mac": "aa:bb:cc:dd:ee:ff",
         "context": "Only powered on for batch jobs."},
        {"name": "hv-01", "platform": "proxmox", "wol_mac": "aa:bb:cc:dd:ee:01",
         "manages_vms": [{"vm_id": 100, "server_name": "vm-01"}]},
        {"name": "vm-01", "platform": "linux"},
        {"name": "hv-02", "platform": "proxmox",
         "manages_vms": [{"vm_id": 200, "server_name": "vm-02"}]},
        {"name": "vm-02", "platform": "linux"},
    ]
}


class TestSystemPrompt:
    def test_no_fleet_details_are_hardcoded(self):
        """The module must contain no server names, addresses or hardware of its own.

        Its predecessor embedded one operator's machines in the prompt string, which is what
        made that prompt unpublishable. Asserted against an empty config so anything appearing
        in the output came from the module itself.
        """
        prompt = build_system_prompt({}, notes="")
        assert "192.168." not in prompt
        assert not any(c.isdigit() for c in prompt.replace("three to five", ""))

    def test_servers_come_from_config(self):
        prompt = build_system_prompt(CONFIG)
        for name in ("web-01", "gpu-01", "hv-01"):
            assert name in prompt

    def test_wol_server_is_marked_on_demand(self):
        """Otherwise a machine that is supposed to be asleep is reported as an outage nightly."""
        prompt = build_system_prompt(CONFIG)
        gpu_line = next(l for l in prompt.splitlines() if l.startswith("- gpu-01"))
        assert "on-demand" in gpu_line

    def test_managed_guest_is_on_demand_without_its_own_wol_mac(self):
        # vm-01 has no wol_mac — it cannot, it is started by `qm` — so its own entry looks like a
        # permanently-on machine unless the manages_vms relationship is followed.
        prompt = build_system_prompt(CONFIG)
        vm_line = next(l for l in prompt.splitlines() if l.startswith("- vm-01"))
        assert "on-demand" in vm_line

    def test_guest_of_an_always_on_hypervisor_is_not_on_demand(self):
        """A 24/7 VM that has crashed must not be described as sleeping normally."""
        prompt = build_system_prompt(CONFIG)
        vm_line = next(l for l in prompt.splitlines() if l.startswith("- vm-02"))
        assert "on-demand" not in vm_line

    def test_always_on_server_is_not_marked_on_demand(self):
        prompt = build_system_prompt(CONFIG)
        web_line = next(l for l in prompt.splitlines() if l.startswith("- web-01"))
        assert "on-demand" not in web_line

    def test_per_server_context_is_included(self):
        assert "Only powered on for batch jobs." in build_system_prompt(CONFIG)

    def test_operator_notes_are_appended(self):
        assert "known benign" in build_system_prompt(CONFIG, notes="known benign warning")

    def test_blank_notes_add_no_empty_section(self):
        assert "Operator notes" not in build_system_prompt(CONFIG, notes="   ")


class TestFormatFindings:
    def test_offline_and_unreachable_read_differently(self):
        """"Asleep" and "did not answer" are different events and must not render alike."""
        out = format_findings([
            LogResult(server="a", success=True, offline=True),
            LogResult(server="b", success=False, error="timed out"),
        ])
        assert "a: offline" in out
        assert "b: could not be reached" in out and "timed out" in out

    def test_clean_server_is_one_line(self):
        out = format_findings([LogResult(server="a", success=True)])
        assert out == "a: clean"

    def test_job_that_did_not_run_is_reported(self):
        out = format_findings([LogResult(server="a", success=True, job_logs={
            "/var/log/backup.log": JobLogResult(ran_today=False, completed=False, last_run="2026-07-30"),
        })])
        assert "did not run today" in out and "2026-07-30" in out

    def test_job_that_started_but_did_not_finish_is_distinct(self):
        out = format_findings([LogResult(server="a", success=True, job_logs={
            "/var/log/backup.log": JobLogResult(ran_today=True, completed=False, last_run="02:00"),
        })])
        assert "did not complete" in out


class TestAnalyze:
    def test_no_model_configured_returns_none(self):
        assert analyze(None, CONFIG, [LogResult(server="a", success=True)]) is None

    def test_model_failure_does_not_propagate(self, monkeypatch):
        """The sweep's findings are the product; the written analysis is commentary on top.

        A model that is down, out of credit, or misconfigured must not take the whole report
        with it.
        """
        def boom(*a, **kw):
            raise LLMError("out of credit")
        monkeypatch.setattr(analysis, "complete", boom)
        llm = LLMConfig(provider="ollama", model="m", base_url="http://x")
        assert analyze(llm, CONFIG, [LogResult(server="a", success=True)]) is None

    def test_findings_and_fleet_reach_the_model(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(analysis, "complete",
                            lambda cfg, system, prompt: seen.update(system=system, prompt=prompt) or "ok")
        llm = LLMConfig(provider="ollama", model="m", base_url="http://x")
        result = analyze(llm, CONFIG, [LogResult(server="web-01", success=True)], notes="a note")
        assert result == "ok"
        assert "web-01" in seen["system"] and "a note" in seen["system"]
        assert "web-01: clean" in seen["prompt"]
