"""Create an isolated Docker PostgreSQL, initialize it, and run synthetic tests."""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.integration.yaml"
DATABASE = "research_agent_integration"


def test_environment(base: dict, port: int, files: Path) -> dict:
    """Override inherited production DSNs and model settings in child processes only."""
    env = dict(base)
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENT_MODEL_KEY", "AGENT_EXPERIMENT_ROOT",
                 "AGENT_EXPERIMENT_TEAM_ID", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    for variable, user in (("MIGRATION_DATABASE_URL", "postgres"), ("AGENT_DATABASE_URL", "agent_runtime"),
                           ("RAG_DATABASE_URL", "rag_runtime"), ("IDENTITY_ADMIN_DATABASE_URL", "identity_admin")):
        password = env["POSTGRES_INTEGRATION_PASSWORD"] if user == "postgres" else secrets.token_hex(24)
        env[variable] = f"postgresql://{user}:{password}@127.0.0.1:{port}/{DATABASE}?connect_timeout=3"
    env.update({"RAG_FILES_DIR": str(files), "RAG_SERVICE_TOKEN": secrets.token_urlsafe(32),
                "AGENT_TOOL_PROVIDERS": "agent_service.tools.rag:tools",
                "AGENT_MODEL": "synthetic-scripted", "AGENT_MODEL_URL": "http://127.0.0.1:9",
                "RAG_BASE_URL": "http://127.0.0.1:9", "RAG_EMBEDDING_URL": "http://127.0.0.1:9",
                "RAG_REWRITE_URL": "", "RAG_RERANK_URL": "", "AGENT_EMBEDDING_URL": "",
                "APP_REVISION": "local-synthetic-integration", "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1", "PYTHONUNBUFFERED": "1"})
    return env


def validate_report(path: Path) -> dict:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    experiment_cases = [case for case in cases if case.get("name", "").startswith((
        "test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes",
        "test_experiment_snapshot_concurrency_and_durable_lease_checks"))]
    if len(experiment_cases) != 3 or any(any(case.find(tag) is not None for tag in
                                           ("skipped", "failure", "error")) for case in experiment_cases):
        raise RuntimeError("The three mandatory experiment database cases did not all pass")
    return {"tests": len(cases), "failures": sum(case.find("failure") is not None for case in cases),
            "errors": sum(case.find("error") is not None for case in cases),
            "skipped": sum(case.find("skipped") is not None for case in cases),
            "experiment_database_cases_passed": len(experiment_cases)}


def run_command(arguments, *, env, log: Path | None = None, timeout=120):
    """Run argv without shell interpolation; retain diagnostics and bounded waits."""
    try:
        result = subprocess.run(arguments, cwd=ROOT, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if log is not None:
            log.write_text(output, encoding="utf-8")
        raise RuntimeError(f"{Path(arguments[0]).name} exceeded the {timeout}s timeout") from None
    if log is not None:
        log.write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        raise RuntimeError(f"{Path(arguments[0]).name} exited with status {result.returncode}")
    return result.stdout


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("experiments", "full"), default="experiments")
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument("--keep-db", action="store_true", help="retain this run's dedicated container for inspection")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    began = time.monotonic()
    project = "rap-test-" + uuid4().hex[:12]
    output = ROOT / ".data" / "postgres-integration" / project
    output.mkdir(parents=True)
    compose_env = dict(os.environ)
    compose_env["POSTGRES_INTEGRATION_PASSWORD"] = secrets.token_hex(24)
    compose_env["POSTGRES_INTEGRATION_PORT"] = str(args.port)
    # Explicit environment and CLI values avoid loading a repository .env file.
    compose = ["docker", "compose", "--env-file", "/dev/null", "-f", str(COMPOSE), "-p", project]
    started = False
    report = {"status": "failed", "project": project, "suite": args.suite,
              "synthetic": True, "real_http_disabled": True, "container_started": False}
    try:
        print("Checking local Docker access...", flush=True)
        run_command(["docker", "info", "--format", "{{.ServerVersion}}"], env=compose_env,
                    log=output / "docker-preflight.log", timeout=15)
        run_command(compose + ["config", "--quiet"], env=compose_env, timeout=15)
        print(f"Starting dedicated PostgreSQL/pgvector on 127.0.0.1:{args.port} ({project})...", flush=True)
        started = True  # Also clean up resources if Compose partially starts and then fails.
        run_command(compose + ["up", "-d", "--wait", "--wait-timeout", "60"], env=compose_env,
                    log=output / "docker-start.log", timeout=180)
        report["container_started"] = True
        env = test_environment(compose_env, args.port, output / "rag-files")
        python = sys.executable
        for step, module in (("migrations", "db.migrate"), ("checkpoints", "agent_service.checkpoints"),
                             ("roles", "db.bootstrap"), ("doctor", "db.doctor")):
            print(f"Initializing {step}...", flush=True)
            run_command([python, "-m", module] + (["--json"] if step == "doctor" else []),
                        env=env, log=output / f"{step}.log", timeout=120)
        report["doctor"] = json.loads((output / "doctor.log").read_text())
        print(f"Running {args.suite} suite with scripted models and real HTTP disabled...", flush=True)
        test_args = ["tests/test_agent_runtime.py", "-k", "experiment"] if args.suite == "experiments" else []
        junit = output / "pytest.xml"
        result = run_command([python, "-m", "scripts.integration_pytest", "-q", "-rs", "-o",
                              "faulthandler_timeout=30", f"--junitxml={junit}", *test_args],
                             env=env, log=output / "pytest.log", timeout=600)
        print(result, end="", flush=True)
        report.update(validate_report(junit))
        if report["failures"] or report["errors"] or report["skipped"]:
            raise RuntimeError("The integration suite must finish with zero failures, errors and skips")
        report["status"] = "passed"
    except (OSError, RuntimeError, ValueError, ET.ParseError) as exc:
        report["error"] = str(exc)
        print(f"Integration run incomplete: {exc}", file=sys.stderr)
    finally:
        if started and not args.keep_db:
            print("Removing this run's dedicated container...", flush=True)
            try:
                run_command(compose + ["down"], env=compose_env, log=output / "docker-cleanup.log", timeout=30)
            except (OSError, RuntimeError) as exc:
                report["cleanup_error"] = str(exc)
                report["status"] = "failed"
        elif started:
            report["database_retained"] = True
            # Retain only in the gitignored run directory, accessible to its owner.
            credentials = output / "docker.env"
            descriptor = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as file:
                file.write(f"POSTGRES_INTEGRATION_PASSWORD={compose_env['POSTGRES_INTEGRATION_PASSWORD']}\n"
                           f"POSTGRES_INTEGRATION_PORT={args.port}\n")
            print(f"Retained container project: {project}; credentials: {credentials}", flush=True)
        report["elapsed_seconds"] = round(time.monotonic() - began, 2)
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"Report directory: {output}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
