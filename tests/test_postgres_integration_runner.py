import asyncio
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

import httpx
import pytest

from scripts.integration_pytest import no_real_http
from scripts import postgres_integration as runner


def test_integration_environment_overrides_production_and_drops_model_credentials(tmp_path):
    base = {name: "production-setting" for name in (
        "MIGRATION_DATABASE_URL", "AGENT_DATABASE_URL", "RAG_DATABASE_URL", "IDENTITY_ADMIN_DATABASE_URL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENT_MODEL_KEY", "AGENT_MODEL_URL", "AGENT_TOOL_PROVIDERS",
        "AGENT_EXPERIMENT_ROOT", "AGENT_EXPERIMENT_TEAM_ID", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    base["POSTGRES_INTEGRATION_PASSWORD"] = "local-only-owner-password"
    env = runner.test_environment(base, 55432, tmp_path)
    for name in ("MIGRATION_DATABASE_URL", "AGENT_DATABASE_URL", "RAG_DATABASE_URL", "IDENTITY_ADMIN_DATABASE_URL"):
        parsed = urlsplit(env[name])
        assert parsed.hostname == "127.0.0.1" and parsed.port == 55432
        assert parsed.path == "/research_agent_integration" and parsed.password
    assert "OPENAI_API_KEY" not in env and "AGENT_MODEL_KEY" not in env
    assert "AGENT_EXPERIMENT_ROOT" not in env and "PYTEST_ADDOPTS" not in env
    assert env["AGENT_MODEL_URL"] == "http://127.0.0.1:9"
    assert base["AGENT_DATABASE_URL"] == "production-setting"


def test_real_http_is_blocked_but_in_memory_transports_still_work():
    with no_real_http() as attempts:
        with httpx.Client() as client, pytest.raises(AssertionError, match="disabled"):
            client.get("https://real-model.invalid")
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"fake": True}))) as client:
            assert client.get("https://mock-model.invalid").json() == {"fake": True}
    assert attempts == ["real-model.invalid"]


def test_async_real_http_is_blocked():
    async def invoke():
        async with httpx.AsyncClient() as client:
            await client.get("https://real-model.invalid")
    with no_real_http() as attempts, pytest.raises(AssertionError, match="disabled"):
        asyncio.run(invoke())
    assert attempts == ["real-model.invalid"]


def xml_report(path, outcome="", count=3):
    names = ["test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes[react]",
             "test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes[plan_execute]",
             "test_experiment_snapshot_concurrency_and_durable_lease_checks"]
    path.write_text("<testsuites><testsuite>" + "".join(f'<testcase name="{name}">{outcome}</testcase>'
                    for name in names[:count]) + "</testsuite></testsuites>")


@pytest.mark.parametrize("outcome,count", [("<skipped/>", 3), ("<failure/>", 3), ("<error/>", 3), ("", 2)])
def test_skipped_failed_or_missing_database_cases_cannot_report_success(tmp_path, outcome, count):
    path = tmp_path / "results.xml"
    xml_report(path, outcome, count)
    with pytest.raises(RuntimeError, match="did not all pass"):
        runner.validate_report(path)


def test_failed_docker_preflight_creates_no_container_and_records_failure(tmp_path, monkeypatch):
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        raise RuntimeError("Docker socket access denied")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "run_command", run)
    assert runner.main([]) == 1
    assert len(calls) == 1 and calls[0][0:2] == ["docker", "info"]
    report = json.loads(next(tmp_path.rglob("summary.json")).read_text())
    assert report["status"] == "failed" and report["container_started"] is False


def test_successful_flow_initializes_database_checks_cases_and_cleans_up(tmp_path, monkeypatch):
    calls = []
    def run(arguments, *, env, log=None, timeout=None):
        calls.append((arguments, env))
        output = '{"status":"ok","checks":[]}' if "db.doctor" in arguments else ""
        if "scripts.integration_pytest" in arguments:
            path = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("--junitxml=")))
            xml_report(path)
            output = "3 passed\n"
        if log:
            log.write_text(output)
        return output
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "run_command", run)
    assert runner.main([]) == 0
    modules = [arguments[2] for arguments, _ in calls if arguments[1:2] == ["-m"]]
    assert modules == ["db.migrate", "agent_service.checkpoints", "db.bootstrap", "db.doctor", "scripts.integration_pytest"]
    assert calls[-1][0][-1] == "down"
    report = json.loads(next(tmp_path.rglob("summary.json")).read_text())
    assert report["status"] == "passed" and report["experiment_database_cases_passed"] == 3


def test_command_timeout_saves_diagnostics_without_hanging(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 1, output=b"docker initialization stalled")
    monkeypatch.setattr(runner.subprocess, "run", timeout)
    log = tmp_path / "timeout.log"
    with pytest.raises(RuntimeError, match="timeout"):
        runner.run_command(["docker", "info"], env={}, log=log, timeout=1)
    assert "stalled" in log.read_text()
