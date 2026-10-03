"""timar's own version: the release check and the upgrade handed to its host."""
import json
import os
import shlex
import stat
import subprocess
import time

import httpx
import pytest

from timar import release, state

LABELS = {
    "com.docker.compose.project": "timar",
    "com.docker.compose.service": "timar",
}


def info(image, workdir="/srv/timar", files=None):
    labels = dict(LABELS, **{"com.docker.compose.project.working_dir": workdir,
                             "com.docker.compose.project.config_files":
                                 ",".join(files or [f"{workdir}/docker-compose.yml"])})
    return {"Config": {"Image": image, "Labels": labels}}


class TestVersions:
    @pytest.mark.parametrize("candidate, than, newer", [
        ("0.2.28", "0.2.27", True),
        ("v0.3.0", "0.2.27", True),
        ("0.2.10", "0.2.9", True),       # numbers, not strings
        ("0.2.27", "0.2.27", False),
        ("0.2.26", "0.2.27", False),
        ("0.3.0-rc1", "0.2.27", False),  # not a release this check offers
        ("0.3.0", "unknown", False),     # a version that cannot be compared offers nothing
    ])
    def test_is_newer(self, candidate, than, newer):
        assert release.is_newer(candidate, than) is newer

    def test_the_check_is_on_unless_switched_off(self):
        assert release.check_enabled({})
        assert release.check_enabled({"check_for_updates": True})
        assert not release.check_enabled({"check_for_updates": False})


class TestLatest:
    @pytest.fixture(autouse=True)
    def empty_cache(self):
        release._cache.clear()
        yield
        release._cache.clear()

    def test_reads_the_release_and_remembers_it(self, monkeypatch):
        calls = []

        def get(url, **kw):
            calls.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url), json={
                "tag_name": "v0.3.0", "html_url": "https://example.test/r", "name": "0.3.0 — x"})
        monkeypatch.setattr(release.httpx, "get", get)
        assert release.latest() == {"version": "0.3.0", "url": "https://example.test/r",
                                    "name": "0.3.0 — x"}
        release.latest()
        assert calls == [release.RELEASES_API]

    def test_a_failed_check_is_none_and_retried_sooner(self, monkeypatch):
        def get(url, **kw):
            raise httpx.ConnectError("no route")
        monkeypatch.setattr(release.httpx, "get", get)
        assert release.latest() is None
        assert release._cache["ttl"] == release.RETRY_AFTER < release.CHECK_EVERY


class TestUpgradeCommand:
    def test_a_pinned_tag_is_pulled_then_rewritten_then_recreated(self):
        cmd = release.upgrade_command(info("ghcr.io/orkun-soylu/timar:0.2.27"), "0.2.28", "0.2.27")
        steps = cmd.split(" && ")
        assert steps[0] == "cd /srv/timar"
        assert steps[1] == "docker pull ghcr.io/orkun-soylu/timar:0.2.28"
        assert "sed -i" in steps[3]
        assert steps[-1] == "docker compose -p timar -f /srv/timar/docker-compose.yml up -d timar"

    def test_latest_is_pulled_through_compose_and_nothing_is_rewritten(self):
        cmd = release.upgrade_command(info("ghcr.io/orkun-soylu/timar:latest"), "0.2.28", "0.2.27")
        assert "sed" not in cmd
        assert "pull timar" in cmd

    @pytest.mark.parametrize("image", ["ghcr.io/orkun-soylu/timar:0.2", "timar:dev",
                                       "registry.local:5000/timar:0.2.27"])
    def test_any_other_image_or_tag_is_refused(self, image):
        with pytest.raises(release.UpgradeError):
            release.upgrade_command(info(image), "0.2.28", "0.2.27")

    def test_without_compose_labels_it_is_refused(self):
        with pytest.raises(release.UpgradeError, match="docker compose"):
            release.upgrade_command({"Config": {"Image": "ghcr.io/orkun-soylu/timar:0.2.27",
                                                "Labels": {}}}, "0.2.28", "0.2.27")

    def _run(self, tmp_path, compose_text, fail_pull=False):
        """Run the generated command for real, with a `docker` that only records its calls."""
        workdir = tmp_path / "timar"
        workdir.mkdir()
        compose = workdir / "docker-compose.yml"
        compose.write_text(compose_text)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "docker"
        fake.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/calls\n'
                        + ('[ "$1" = pull ] && exit 1\n' if fail_pull else "") + "exit 0\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        cmd = release.upgrade_command(info("ghcr.io/orkun-soylu/timar:0.2.27", str(workdir)),
                                      "0.2.28", "0.2.27")
        result = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True,
                                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
        calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
        return result, compose.read_text(), calls

    def test_the_rewrite_touches_only_that_exact_tag(self, tmp_path):
        text = ('services:\n  timar:\n    # was ghcr.io/orkun-soylu/timar:0.2.270 once\n'
                '    image: "ghcr.io/orkun-soylu/timar:0.2.27"\n')
        result, after, calls = self._run(tmp_path, text)
        assert result.returncode == 0, result.stderr
        assert 'image: "ghcr.io/orkun-soylu/timar:0.2.28"' in after
        assert "timar:0.2.270 once" in after
        assert calls.splitlines()[-1].endswith("up -d timar")

    def test_a_failed_pull_changes_nothing(self, tmp_path):
        text = "services:\n  timar:\n    image: ghcr.io/orkun-soylu/timar:0.2.27\n"
        result, after, calls = self._run(tmp_path, text, fail_pull=True)
        assert result.returncode != 0
        assert after == text
        assert "up -d" not in calls

    def test_a_tag_the_files_do_not_name_stops_before_recreating(self, tmp_path):
        """A tag set through a variable: the container runs 0.2.27, the file says ${TAG}."""
        text = "services:\n  timar:\n    image: ghcr.io/orkun-soylu/timar:${TAG}\n"
        result, after, calls = self._run(tmp_path, text)
        assert result.returncode == 3
        assert after == text
        assert "up -d" not in calls


class TestStatus:
    @pytest.fixture(autouse=True)
    def data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIMAR_DATA", str(tmp_path))

    def _pending(self, **kw):
        state.save({"upgrade": {"from": "0.2.27", "target": "0.2.28", "host": "docker-01",
                                "unit": "timar-upgrade-1", "started": time.time(), **kw}})

    def test_nothing_started_is_none(self):
        assert release.upgrade_status({}) is None

    def test_the_new_version_answering_is_done_and_then_forgotten(self, monkeypatch):
        self._pending()
        monkeypatch.setattr(release, "current_version", lambda: "0.2.28")
        assert release.upgrade_status({})["state"] == "done"
        assert release.upgrade_status({}) is None

    def test_a_failed_handoff_reports_its_log(self, monkeypatch):
        self._pending()
        cfg = {"servers": [{"name": "docker-01", "host": "10.0.0.6", "user": "op"}]}
        monkeypatch.setattr(release, "current_version", lambda: "0.2.27")
        monkeypatch.setattr(release, "is_host_up", lambda host: True)
        monkeypatch.setattr(release.config, "resolve_ssh_key", lambda s: "key")

        class FakeSSH:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(release, "connect", lambda *a, **kw: FakeSSH())
        monkeypatch.setattr(release.updater, "handoff_status",
                            lambda ssh, unit, **kw: ("done", 1, "manifest unknown"))
        result = release.upgrade_status(cfg)
        assert result["state"] == "failed" and "manifest unknown" in result["error"]
        assert "upgrade" not in state.load()

    def test_no_answer_for_too_long_is_a_failure(self, monkeypatch):
        self._pending(started=time.time() - release.UPGRADE_GIVE_UP - 1)
        monkeypatch.setattr(release, "current_version", lambda: "0.2.27")
        assert release.upgrade_status({})["state"] == "failed"


class TestSettingsPage:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        import importlib
        from fastapi.testclient import TestClient
        monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
        from timar import config
        from timar.web import app as app_module, auth
        importlib.reload(config)
        importlib.reload(auth)
        importlib.reload(app_module)
        auth._failures.clear()
        client = TestClient(app_module.app, follow_redirects=False)
        client.post("/setup", data={"username": "op", "password": "correct-horse-battery"})
        release._cache.clear()
        yield client
        release._cache.clear()

    def test_the_page_names_the_version_and_links_to_the_project(self, client):
        page = client.get("/settings").text
        assert release.current_version() in page
        assert f'href="{release.SOURCE_URL}"' in page and f'href="{release.SITE_URL}"' in page
        assert 'hx-get="/settings/version"' in page

    def test_a_newer_release_offers_the_upgrade(self, client, monkeypatch):
        monkeypatch.setattr(release, "current_version", lambda: "0.2.27")
        monkeypatch.setattr(release, "latest", lambda fresh=False: {
            "version": "0.2.28", "url": "https://example.test/r", "name": "0.2.28"})
        fragment = client.get("/settings/version").text
        assert "0.2.28" in fragment and 'hx-post="/settings/upgrade"' in fragment

    def test_with_the_check_off_github_is_not_asked(self, client, monkeypatch):
        from timar import config
        config.save({"check_for_updates": False})
        monkeypatch.setattr(release.httpx, "get", lambda *a, **kw: pytest.fail("asked GitHub"))
        assert "update check is off" in client.get("/settings/version").text

    def test_no_upgrade_while_a_job_runs(self, client, monkeypatch):
        from timar.web import settings
        monkeypatch.setattr(settings.scheduler, "is_running", lambda name: name == "update")
        monkeypatch.setattr(release, "start_upgrade", lambda *a: pytest.fail("started"))
        monkeypatch.setattr(release, "latest", lambda fresh=False: None)
        assert "Wait for" in client.post("/settings/upgrade", data={"version": "9.9.9"}).text

    def test_the_upgrade_is_behind_the_login(self, client):
        client.post("/logout")
        client.cookies.clear()
        assert client.post("/settings/upgrade", data={"version": "9.9.9"}).status_code in (303, 401)
