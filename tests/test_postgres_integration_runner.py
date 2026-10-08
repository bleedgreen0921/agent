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


@pytest.mark.parametrize("junit_failure", ["truncated", "unreadable", "disappeared", "missing", "valid"])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_failed_pytest_retains_summary_and_original_error(tmp_path, monkeypatch, junit_failure, cleanup_failure):
    calls = []
    parse = runner.ET.parse

    def read_report(path):
        if junit_failure == "unreadable":
            raise PermissionError("JUnit read denied")
        if junit_failure == "disappeared":
            raise FileNotFoundError("JUnit disappeared during read")
        return parse(path)

    def run(arguments, *, env, log=None, timeout=None):
        calls.append(arguments)
        output = '{"status":"ok","checks":[]}' if "db.doctor" in arguments else ""
        if log:
            log.write_text(output)
        if "scripts.integration_pytest" in arguments:
            path = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("--junitxml=")))
            if junit_failure == "truncated":
                path.write_text("<testsuites><testsuite><testcase")
            elif junit_failure != "missing":
                xml_report(path, "<failure/>")
            raise RuntimeError("pytest exceeded the 600s timeout")
        if arguments[-1] == "down" and cleanup_failure:
            raise RuntimeError("Docker cleanup failed")
        return output

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "run_command", run)
    monkeypatch.setattr(runner.ET, "parse", read_report)
    assert runner.main([]) == 1
    report = json.loads(next(tmp_path.rglob("summary.json")).read_text())
    assert report["status"] == "failed" and report["error"] == "pytest exceeded the 600s timeout"
    assert report["container_started"] is True and report["elapsed_seconds"] >= 0
    assert calls[-1][-1] == "down"
    if cleanup_failure:
        assert report["cleanup_error"] == "Docker cleanup failed" and "database_cleaned_up" not in report
    else:
        assert report["database_cleaned_up"] is True
    if junit_failure in {"truncated", "unreadable", "disappeared"}:
        assert report["junit_error"] and "tests" not in report
    elif junit_failure == "valid":
        assert report["tests"] == report["failures"] == 3 and "junit_error" not in report
    else:
        assert "tests" not in report and "junit_error" not in report


def test_final_junit_read_failure_cannot_report_success(tmp_path, monkeypatch):
    parse = runner.ET.parse
    reads = 0

    def read_report(path):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError("JUnit no longer readable")
        return parse(path)

    def run(arguments, *, env, log=None, timeout=None):
        output = '{"status":"ok","checks":[]}' if "db.doctor" in arguments else ""
        if "scripts.integration_pytest" in arguments:
            path = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("--junitxml=")))
            xml_report(path)
        if log:
            log.write_text(output)
        return output

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "run_command", run)
    monkeypatch.setattr(runner.ET, "parse", read_report)
    assert runner.main([]) == 1
    report = json.loads(next(tmp_path.rglob("summary.json")).read_text())
    assert report["status"] == "failed" and report["junit_error"] == "OSError: JUnit no longer readable"
    assert report["error"] and report["database_cleaned_up"] is True and report["elapsed_seconds"] >= 0
    assert report["tests"] == report["experiment_database_cases_passed"] == 3


@pytest.mark.parametrize("outcome,missing", [("<skipped/>", False), ("<failure/>", False), ("<error/>", False), ("", True)])
def test_research_requires_all_seven_database_scenarios(tmp_path, outcome, missing):
    path = tmp_path / "research.xml"
    xml_report(path)
    body = path.read_text().replace("</testsuite></testsuites>", "")
    names = [f"test_experiment_plots_publish_and_resume_in_both_modes[{mode}]" for mode in ("react", "plan_execute")]
    names += [f"test_synthetic_research_real_rag_mixed_citations_and_resume[{mode}]" for mode in ("react", "plan_execute")]
    body += "".join(f'<testcase name="{name}">{outcome}</testcase>' for name in (names[:-1] if missing else names))
    path.write_text(body + "</testsuite></testsuites>")
    with pytest.raises(RuntimeError, match="did not all pass"):
        runner.validate_report(path, research=True)


def test_research_report_counts_seven_actual_scenarios(tmp_path):
    path = tmp_path / "research.xml"
    xml_report(path)
    body = path.read_text().replace("</testsuite></testsuites>", "")
    body += "".join(f'<testcase name="{prefix}[{mode}]"/>' for prefix in (
        "test_experiment_plots_publish_and_resume_in_both_modes", "test_synthetic_research_real_rag_mixed_citations_and_resume")
        for mode in ("react", "plan_execute"))
    path.write_text(body + "</testsuite></testsuites>")
    report = runner.validate_report(path, research=True)
    assert report["tests"] == 7 and report["plot_database_cases_passed"] == report["research_database_cases_passed"] == 2
