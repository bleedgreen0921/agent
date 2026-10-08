"""Real PostgreSQL, uploads, worker, retrieval SQL and Agent; only models are scripted."""

import hashlib
import fcntl
import json
import os
import re
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from agent_service import execution
from agent_service.checkpoints import with_agent_search_path
from agent_service.rag_client import RagClient
from agent_service.runs import get_run
from agent_service.runtime import claim_run, safe_to_resume
from agent_service.tools import rag as rag_tools, experiments, experiment_plots
from agent_service.tooling import FatalToolError, ToolExecutionContext
from contracts.v1 import RunResponse
from db.connection import connect
from rag_service import chunking, retrieval, worker
from rag_service.app import app as rag_app
from scripts import synthetic_research
from scripts.synthetic_experiments import generate
from test_agent_runtime import ScriptedModel, credential, insert_run, quiesce_runs

pytestmark = pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")
BASE, ATTENTION = "20261001_090000", "20261001_103000"


class OffsetTokenizer:
    def spans(self, text):
        return [(match.start(), match.end()) for match in re.finditer(r"[A-Za-z0-9_]+|[^\s]", text)]

    def encode(self, text, **kwargs):
        return list(range(len(self.spans(text))))

    def __call__(self, text, **kwargs):
        return {"offset_mapping": self.spans(text)}


def requests(*, documents=False):
    calls = [
        ("search_experiments", {"filters": {"model.attention.enabled": True}}),
        ("find_experiment_controls", {"baseline_id": BASE, "changed_paths": ["model.attention.enabled"]}),
        ("compare_experiment_metrics", {"left_id": BASE, "right_id": ATTENTION,
                                         "changed_paths": ["model.attention.enabled"]}),
        ("plot_experiment_accuracy", {"experiment_ids": [BASE, ATTENTION]}),
        ("plot_experiment_confusion", {"experiment_id": BASE}),
    ]
    if documents:
        calls.insert(0, ("search_evidence", {"query": "Synthetic", "top_k": 10}))
    return calls


def messages(calls):
    return [*[AIMessage(content="", tool_calls=[{"name": name, "args": args,
                    "id": f"research-{index}", "type": "tool_call"}])
              for index, (name, args) in enumerate(calls)], AIMessage(content="Synthetic research tools complete")]


class ResearchModel(ScriptedModel):
    def with_structured_output(self, schema, **kwargs):
        if schema is not execution.FinalDraft:
            return super().with_structured_output(schema, **kwargs)

        def value(messages):
            available = json.loads(messages[-1][1])["available_evidence"]
            by_title = {item["title"]: item for item in available}
            claims = []
            if "compare_experiment_metrics" in by_title:
                item = by_title["compare_experiment_metrics"]
                data = json.loads(item["content"])["data"]
                overall, low = data["aggregates"]["overall"], data["aggregates"]["low_snr"]
                claims.append(execution.DraftClaim(
                    text=f"合成单对实验总体准确率 {100*overall['left_accuracy']:g}%→{100*overall['right_accuracy']:g}%，"
                         f"差异 {overall['delta_percentage_points']:g} 个百分点；低 SNR 差异 {low['delta_percentage_points']:g} 个百分点。",
                    support="evidence", evidence_ids=[item["evidence_id"]]))
            for title in ("plot_experiment_accuracy", "plot_experiment_confusion"):
                if title in by_title:
                    item = by_title[title]
                    data = json.loads(item["content"])["data"]
                    paths = [artifact["path"] for artifact in data["artifacts"] if artifact["media_type"].startswith("image/")]
                    claims.append(execution.DraftClaim(text=f"合成图片本地文件路径（不是下载链接）：{', '.join(paths)}。",
                                                       support="evidence", evidence_ids=[item["evidence_id"]]))
            docs = [item for item in available if item.get("document_id") and item["title"].startswith("Synthetic research")]
            for item in docs:
                if "attention" in item["title"]:
                    text = "合成方法文档仅提出待检验的注意力研究假设。"
                elif "evaluation" in item["title"]:
                    text = "评测按样本加权，低 SNR 为 <= -5 dB；描述性单对比较不构成统计显著性或因果证明。"
                else:
                    text = "合成研究记录声称提高 20 个百分点，与实际计数不符，不能作为事实。"
                ids = [item["evidence_id"]]
                if "record" in item["title"] and "compare_experiment_metrics" in by_title:
                    ids.append(by_title["compare_experiment_metrics"]["evidence_id"])
                claims.append(execution.DraftClaim(text=text, support="evidence", evidence_ids=ids))
            claims.append(execution.DraftClaim(text="材料均为合成数据，单对比较不构成显著性或因果证明。",
                                               support="unverified", evidence_ids=[], reason="Fixed synthetic acceptance scope and interpretation limit"))
            return execution.FinalDraft(answer="\n".join(claim.text for claim in claims), claims=claims)
        return RunnableLambda(value)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    quiesce_runs()
    team, _ = credential()
    root = tmp_path / "inputs"
    generate(root)
    output = tmp_path / "plots"
    monkeypatch.setenv("AGENT_EXPERIMENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_EXPERIMENT_TEAM_ID", str(team))
    monkeypatch.setenv("AGENT_EXPERIMENT_PLOT_ROOT", str(output))
    monkeypatch.setenv("AGENT_TOOL_PROVIDERS", "agent_service.tools.experiments:tools,agent_service.tools.experiment_plots:tools")
    monkeypatch.setenv("AGENT_MODEL", "scripted-research")
    return team, root, output


def execute_with_resume(monkeypatch, team, mode, calls):
    model = ResearchModel(responses=messages(calls))
    monkeypatch.setattr(execution, "model", lambda: model)
    run_id = insert_run(team, created_delta=timedelta(days=-1))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET mode=%s,task=%s WHERE id=%s", (mode, synthetic_research.TASK, run_id))
    claimed = claim_run()
    assert claimed["run_id"] == run_id
    final_generate = execution.final_generate
    restored = []
    def stop_before_final(*args, **kwargs):
        context = args[2]
        restored.append(dict(context.evidence))
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("UPDATE agent.agent_runs SET leased_until=now()-interval '1 second' WHERE id=%s", (run_id,))
        raise PermissionError("Synthetic worker interruption before final generation")
    with monkeypatch.context() as patch:
        patch.setattr(execution, "final_generate", stop_before_final)
        execution.execute_run(run_id, claimed["lease_token"])
    with connect("AGENT_DATABASE_URL") as conn:
        assert safe_to_resume(conn, run_id)
        rows = conn.execute("SELECT status,checkpointed,evidence_ids FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchall()
        assert len(rows) == len(calls) and all(row["status"] == "succeeded" and row["checkpointed"] for row in rows)
    next_claim = claim_run()
    assert next_claim["run_id"] == run_id and not next_claim["first_claim"]
    assert next_claim["lease_token"] != claimed["lease_token"]
    def inspect_restore(*args, **kwargs):
        assert args[2].evidence == restored[0]
        return final_generate(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(execution, "final_generate", inspect_restore)
        execution.execute_run(run_id, next_claim["lease_token"])
    result = RunResponse.model_validate(get_run(run_id, team))
    assert result.status == "completed", result.model_dump()
    assert result.result is not None
    with connect("AGENT_DATABASE_URL") as conn:
        count = conn.execute("SELECT count(*) AS n FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()["n"]
        assert count == len(calls)  # Recovery uses checkpoints, never re-executes the tools.
        snapshot = conn.execute("SELECT snapshot FROM agent.run_experiment_snapshots WHERE run_id=%s", (run_id,)).fetchone()["snapshot"]
        evidence_rows = conn.execute("""SELECT e.evidence_id,e.content,e.source_locator FROM agent.evidence_snapshots e
            JOIN agent.run_results r ON r.id=e.result_id WHERE r.run_id=%s""", (run_id,)).fetchall()
    assert {row["evidence_id"] for row in evidence_rows} == {item.evidence_id for item in result.result.citations}
    for row in evidence_rows:
        assert row["content"] == restored[0][row["evidence_id"]]["content"]
        assert row["source_locator"] == restored[0][row["evidence_id"]]["source_locator"]
    return run_id, result.result, snapshot, restored[0], claimed["lease_token"]


def verify_plots(run_id, result, snapshot, root, output):
    plots = [item for item in result.citations if item.title.startswith("plot_experiment_")]
    assert len(plots) == 2
    for citation in plots:
        locator = citation.source_locator
        assert locator.method_version == "1" and locator.snapshot_sha256 == snapshot["snapshot_sha256"]
        for source in locator.source_files:
            assert hashlib.sha256((root / source.path).read_bytes()).hexdigest() == source.sha256
        data = json.loads(citation.content)["data"]
        for artifact in data["artifacts"]:
            assert str(run_id) in artifact["path"]
            raw = (output / artifact["path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == artifact["sha256"] and len(raw) == artifact["size_bytes"]
        manifest = json.loads((output / data["artifacts"][-1]["path"]).read_text())
        assert manifest["run_id"] == str(run_id) and manifest["snapshot_sha256"] == snapshot["snapshot_sha256"]
        with connect("AGENT_DATABASE_URL") as conn:
            trace = conn.execute("SELECT evidence_ids,status FROM agent.tool_calls WHERE id=%s AND run_id=%s",
                                 (UUID(manifest["first_tool_call_id"]), run_id)).fetchone()
        assert trace["status"] == "succeeded" and citation.evidence_id in trace["evidence_ids"]
        if data["kind"] == "confusion":
            assert sum(map(sum, data["counts"])) == 800
            assert sum(data["counts"][i][i] for i in range(4)) == 560
        else:
            assert sum(point["n_correct"] for point in data["curves"][0]["points"]) == 560
            assert sum(point["n_correct"] for point in data["curves"][1]["points"]) == 604
    assert not list(output.rglob(".tmp-*"))


@pytest.mark.parametrize("mode", ["react", "plan_execute"])
def test_experiment_plots_publish_and_resume_in_both_modes(configured, monkeypatch, mode):
    team, root, output = configured
    run_id, result, snapshot, evidence, token = execute_with_resume(monkeypatch, team, mode, requests())
    verify_plots(run_id, result, snapshot, root, output)
    assert "5.5 个百分点" in result.answer and "本地文件路径" in result.answer
    settings = experiments.Settings(root, team)
    forged = ToolExecutionContext(run_id, token, team_id=uuid4())
    with pytest.raises(FatalToolError):
        experiments._inputs(types.SimpleNamespace(context=forged, tool_call_id="forged"), settings)
    with pytest.raises(PermissionError):
        experiments.load_snapshot(ToolExecutionContext(run_id, token, team_id=team), settings)
    (root / "experiments" / BASE / "config.yaml").write_text("modified after publication")
    assert RunResponse.model_validate(get_run(run_id, team)).result.citations == result.citations


@pytest.fixture
def research_documents(configured, monkeypatch, tmp_path):
    team, root, output = configured
    material = tmp_path / "research"
    synthetic_research.generate(material)
    monkeypatch.setenv("RAG_FILES_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    monkeypatch.setenv("RAG_BASE_URL", "http://testserver")
    monkeypatch.setenv("AGENT_TOOL_PROVIDERS", "agent_service.tools.rag:tools,agent_service.tools.experiments:tools,agent_service.tools.experiment_plots:tools")
    monkeypatch.setattr(chunking, "tokenizer", lambda: OffsetTokenizer())
    def embed(texts, model, dimensions):
        return [[1.0] + [0.0] * (dimensions - 1) for _ in texts]
    monkeypatch.setattr(worker, "embed", embed)
    monkeypatch.setattr(retrieval, "embed", embed)
    monkeypatch.setattr(retrieval, "rewrite", lambda query: query)
    def rerank(query, contents):
        return [{"index": index, "score": 100.0 if "Synthetic data / 合成资料" in content else 0.0}
                for index, content in enumerate(contents)]
    monkeypatch.setattr(retrieval, "rerank", rerank)
    _, admin_key = credential("admin")
    admin = {"Authorization": "Bearer " + admin_key}
    documents = {}
    with TestClient(rag_app) as client:
        for path in sorted((material / "documents").glob("*.md")):
            response = client.post("/v1/admin/documents", headers={**admin, "Idempotency-Key": uuid4().hex},
                data={"title": "Synthetic research " + path.stem, "visibility": "restricted", "team_ids": json.dumps([str(team)])},
                files={"file": (path.name, path.read_bytes(), "text/markdown")})
            assert response.status_code == 202, response.text
            info = response.json()
            job = worker.claim()
            assert job is not None and str(job["version_id"]) == info["document_version_id"]
            worker.process(job)
            status = client.get(f"/v1/admin/documents/{info['document_id']}/versions/{info['document_version_id']}", headers=admin).json()
            assert status["status"] == "active", status
            documents["doc_" + info["document_id"]] = (path, info)
        # Inject the in-process HTTP transport into the real client; all client methods and API/SQL remain intact.
        def client_factory(base_url, service_token, **kwargs):
            return RagClient(base_url, service_token, client=TestClient(rag_app), **kwargs)
        monkeypatch.setattr(rag_tools, "RagClient", client_factory)
        yield team, root, output, material, client, admin, documents
        for _, info in documents.values():
            assert client.delete("/v1/admin/documents/" + info["document_id"], headers=admin).status_code == 200


@pytest.mark.parametrize("mode", ["react", "plan_execute"])
def test_synthetic_research_real_rag_mixed_citations_and_resume(research_documents, monkeypatch, mode):
    team, root, output, material, client, admin, documents = research_documents
    run_id, result, snapshot, evidence, _ = execute_with_resume(monkeypatch, team, mode, requests(documents=True))
    verify_plots(run_id, result, snapshot, root, output)
    expected = json.loads((material / "expected.json").read_text())  # Assertions only; never retrieval/calculation.
    comparison = next(item for item in result.citations if item.title == "compare_experiment_metrics")
    data = json.loads(comparison.content)["data"]
    assert data["aggregates"]["overall"] == {"left_accuracy": expected["numbers"]["baseline_accuracy"],
        "right_accuracy": expected["numbers"]["attention_accuracy"], "delta_percentage_points": 5.5}
    assert data["aggregates"]["low_snr"]["delta_percentage_points"] == 11
    for fragment in ("70%→75.5%", "5.5 个百分点", "11 个百分点", "与实际计数不符", "不构成统计显著性或因果证明", "合成数据"):
        assert fragment in result.answer
    doc_citations = [item for item in result.citations if item.document_id]
    assert {item.document_id for item in doc_citations} == set(documents)
    for item in doc_citations:
        path, _ = documents[item.document_id]
        loc = item.source_locator
        assert loc.kind == "markdown"
        source = "\n".join(path.read_text().splitlines()[loc.line_start-1:loc.line_end])
        assert expected["expected_fragments"][path.name] in item.content and expected["expected_fragments"][path.name] in source
        assert item.evidence_id in evidence
    with connect("RAG_DATABASE_URL") as conn:
        audits = conn.execute("SELECT * FROM rag.retrieval_audit WHERE run_id=%s AND operation='search'", (str(run_id),)).fetchall()
    assert len(audits) == 1 and audits[0]["team_id"] == team and audits[0]["status"] == "ok"
    stages = audits[0]["candidate_trace"]["stages"]
    assert stages["dense"]["candidates"] and stages["fts"]["candidates"]
    assert {item.evidence_id for item in doc_citations} <= set(audits[0]["selected_evidence_ids"])
    with connect("AGENT_DATABASE_URL") as conn:
        trace = conn.execute("SELECT evidence_ids,retrieval_id,service_request_id FROM agent.tool_calls WHERE id=%s",
                             (UUID(audits[0]["tool_call_id"]),)).fetchone()
    assert trace["retrieval_id"] == audits[0]["retrieval_id"] and trace["service_request_id"] == audits[0]["request_id"]
    for claim in result.claims:
        if "评测按样本" in claim.text or "方法文档" in claim.text:
            assert set(claim.evidence_ids) <= {item.evidence_id for item in doc_citations}
        if "总体准确率" in claim.text or "图片本地" in claim.text:
            assert all(evidence[item]["source_locator"]["kind"] == "experiment" for item in claim.evidence_ids)
    foreign_team, _ = credential()
    headers = {"Authorization": "Bearer " + "s" * 43, "X-Team-Id": str(foreign_team),
               "X-Run-Id": str(uuid4()), "X-Tool-Call-Id": str(uuid4())}
    for item in doc_citations:
        assert client.get("/v1/evidence/" + item.evidence_id, headers=headers).status_code == 404
    # Delete/replace original materials; historical Run snapshots remain stable.
    first_path, first_info = next(iter(documents.values()))
    replacement = client.post(f"/v1/admin/documents/{first_info['document_id']}/versions", headers=admin,
                             files={"file": ("replacement.md", b"# Synthetic replacement\n\nChanged source.", "text/markdown")})
    assert replacement.status_code == 202
    job = worker.claim()
    assert job is not None
    worker.process(job)
    (root / "experiments" / BASE / "config.yaml").write_text("changed after publication")
    assert RunResponse.model_validate(get_run(run_id, team)).result.citations == result.citations


@pytest.mark.parametrize("scenario", ["no_hits", "unavailable", "invalid_control", "plot_failed"])
def test_research_abnormal_observations_never_publish_invented_citations(research_documents, monkeypatch, scenario):
    team, root, output, material, client, admin, documents = research_documents
    temporary_revision = None
    if scenario == "no_hits":
        # Empty revision keeps genuine dense SQL; the unique query also has no sparse match.
        temporary_revision = uuid4()
        with connect("RAG_DATABASE_URL") as conn:
            previous = conn.execute("SELECT active_revision_id FROM rag.index_state").fetchone()["active_revision_id"]
            conn.execute("""INSERT INTO rag.index_revisions(id,model,dimensions,document_template,query_template,state)
                VALUES (%s,'scripted-empty',1024,'test','test','active')""", (temporary_revision,))
            conn.execute("UPDATE rag.index_state SET active_revision_id=%s", (temporary_revision,))
        calls = [("search_evidence", {"query": "zzresearchnonexistent"})]
    elif scenario == "unavailable":
        # A superseded active pointer exercises the genuine index-unavailable API response.
        with connect("RAG_DATABASE_URL") as conn:
            previous = conn.execute("SELECT active_revision_id FROM rag.index_state").fetchone()["active_revision_id"]
            conn.execute("UPDATE rag.index_revisions SET state='superseded' WHERE id=%s", (previous,))
        calls = [("search_evidence", {"query": "Synthetic research"})]
    elif scenario == "invalid_control":
        calls = [("compare_experiment_metrics", {"left_id": BASE, "right_id": "20261001_133000",
                                                   "changed_paths": ["model.attention.enabled"]})]
    else:
        from experiment_service import plotting
        monkeypatch.setattr(plotting, "render", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic render failure")))
        calls = [("plot_experiment_accuracy", {"experiment_ids": [BASE, ATTENTION]})]
    model = ResearchModel(responses=messages(calls))
    monkeypatch.setattr(execution, "model", lambda: model)
    run_id = insert_run(team, created_delta=timedelta(days=-1))
    claimed = claim_run()
    assert claimed["run_id"] == run_id
    try:
        execution.execute_run(run_id, claimed["lease_token"])
        response = RunResponse.model_validate(get_run(run_id, team))
        assert response.status == "completed" and response.result.citations == []
        assert all(not claim.evidence_ids and claim.support == "unverified" for claim in response.result.claims)
        with connect("AGENT_DATABASE_URL") as conn:
            trace = conn.execute("SELECT status,evidence_ids,error_code,result_summary FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
        assert not trace["evidence_ids"]
        if scenario in {"plot_failed", "invalid_control"}:
            assert trace["status"] == "failed"
            assert trace["error_code"] == ("EXPERIMENT_PLOT_FAILED" if scenario == "plot_failed" else "INCOMPARABLE_EXPERIMENTS")
        elif scenario == "unavailable":
            assert trace["status"] == "degraded"
        else:
            assert trace["result_summary"]["status"] == "no_hits"
        assert not list(output.rglob("plot.png")) and not list(output.rglob(".tmp-*"))
    finally:
        if scenario in {"no_hits", "unavailable"}:
            with connect("RAG_DATABASE_URL") as conn:
                conn.execute("UPDATE rag.index_state SET active_revision_id=%s", (previous,))
                conn.execute("UPDATE rag.index_revisions SET state='active' WHERE id=%s", (previous,))
                if temporary_revision:
                    conn.execute("DELETE FROM rag.index_revisions WHERE id=%s", (temporary_revision,))


@pytest.mark.parametrize("revocation", ["lease", "deadline", "cancel"])
def test_experiment_plot_publication_rechecks_real_run_after_render(configured, monkeypatch, revocation):
    from langchain.tools import ToolRuntime
    from experiment_service import plotting

    team, root, output = configured
    run_id = insert_run(team, created_delta=timedelta(days=-1))
    claimed = claim_run()
    context = ToolExecutionContext(run_id, claimed["lease_token"], team_id=team)
    context.tool_call_ids["revoked"] = uuid4()
    runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda _: None,
                          tool_call_id="revoked", store=None)
    original_render = plotting.render
    def revoke(data, directory):
        original_render(data, directory)
        assignment = {"lease": "leased_until=now()-interval '1 second'",
                      "deadline": "execution_deadline_at=now()-interval '1 second'", "cancel": "status='cancelling'"}[revocation]
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute(f"UPDATE agent.agent_runs SET {assignment} WHERE id=%s", (run_id,))
    monkeypatch.setattr(plotting, "render", revoke)
    tool = experiment_plots.tools().tools[0].tool
    with pytest.raises(PermissionError):
        tool.func(experiment_ids=[BASE], runtime=runtime)
    assert not context.evidence and not context.tool_metadata and not context.notices
    assert not list(output.rglob("plot.png")) and not list(output.rglob(".tmp-*"))


def wait_for_publication_condition(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("Timed out waiting for the publication race condition")


@pytest.mark.parametrize("expiry", ["leased_until", "execution_deadline_at"])
@pytest.mark.parametrize("lock_kind,existing", [("row", False), ("file", False), ("file", True)])
def test_experiment_plot_lock_wait_expiry_rejects_publication(configured, monkeypatch, expiry, lock_kind, existing):
    from langchain.tools import ToolRuntime
    from experiment_service import plotting

    team, root, output = configured
    run_id = insert_run(team, created_delta=timedelta(days=-1))
    claimed = claim_run()
    context = ToolExecutionContext(run_id, claimed["lease_token"], team_id=team)
    context.tool_call_ids["lock-wait"] = uuid4()
    runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda _: None,
                          tool_call_id="lock-wait", store=None)
    experiments.load_snapshot(context, experiments.settings_from_env())
    tool = experiment_plots.tools().tools[0].tool

    def invoke():
        return tool.func(experiment_ids=[BASE], runtime=runtime)

    if existing:
        invoke()
        context.evidence.clear()
        context.tool_metadata.clear()
        context.notices.clear()
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    ready, proceed, file_waiting = threading.Event(), threading.Event(), threading.Event()
    authorize = experiments._authorize_run
    flock = fcntl.flock
    publisher_pid = []
    held_fd = None

    def gated_authorize(conn, context, settings, *, lock=False):
        if lock:
            publisher_pid.append(conn.info.backend_pid)
            ready.set()  # Staging and candidate validation are already complete.
            assert proceed.wait(10), "Timed out releasing the publication gate"
        return authorize(conn, context, settings, lock=lock)

    def observed_flock(fd, operation):
        if lock_kind == "file" and operation == fcntl.LOCK_EX and proceed.is_set() and fd != held_fd:
            # Prove contention before entering the real blocking flock call.
            with pytest.raises(BlockingIOError):
                flock(fd, operation | fcntl.LOCK_NB)
            file_waiting.set()
        return flock(fd, operation)

    monkeypatch.setattr(experiments, "_authorize_run", gated_authorize)
    monkeypatch.setattr(plotting.fcntl, "flock", observed_flock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(invoke)
        try:
            assert ready.wait(10), "Publisher never reached the authorization gate"
            with connect("AGENT_DATABASE_URL") as observer:
                # Start the short validity window after rendering, before either lock wait.
                observer.execute(f"UPDATE agent.agent_runs SET {expiry}=clock_timestamp()+interval '2 seconds' WHERE id=%s",
                                 (run_id,))
                observer.commit()
                if lock_kind == "row":
                    # Leave the row unchanged while the publisher waits on our lock.
                    observer.execute("SELECT id FROM agent.agent_runs WHERE id=%s FOR UPDATE", (run_id,))
                else:
                    held_fd = os.open(output / str(team) / str(run_id), os.O_RDONLY | os.O_DIRECTORY)
                    flock(held_fd, fcntl.LOCK_EX)
                try:
                    proceed.set()
                    if lock_kind == "row":
                        wait_for_publication_condition(lambda: bool(observer.execute(
                            "SELECT pg_blocking_pids(%s) AS blockers", (publisher_pid[0],)).fetchone()["blockers"]))
                    else:
                        assert file_waiting.wait(5), "Publisher did not wait on the file lock"
                    wait_for_publication_condition(lambda: observer.execute(
                        f"SELECT {expiry}<=clock_timestamp() AS expired FROM agent.agent_runs WHERE id=%s",
                        (run_id,)).fetchone()["expired"])
                    assert not future.done(), "Publisher did not remain blocked until expiry"
                finally:
                    if held_fd is not None:
                        flock(held_fd, fcntl.LOCK_UN)
                        os.close(held_fd)
                        held_fd = None
                    observer.rollback()
            with pytest.raises(PermissionError, match="lease was revoked"):
                future.result(timeout=10)
        finally:
            proceed.set()
    assert not context.evidence and not context.tool_metadata and not context.notices
    assert not list(output.rglob(".tmp-*"))
    assert before == {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}


def test_experiment_plot_concurrent_real_run_reuses_and_rejects_corruption(configured):
    from concurrent.futures import ThreadPoolExecutor
    from langchain.tools import ToolRuntime
    from agent_service.tooling import RecoverableToolError

    team, root, output = configured
    run_id = insert_run(team, created_delta=timedelta(days=-1))
    claimed = claim_run()
    provider = experiment_plots.tools()
    def invoke(index):
        context = ToolExecutionContext(run_id, claimed["lease_token"], team_id=team)
        context.tool_call_ids[str(index)] = uuid4()
        runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda _: None,
                              tool_call_id=str(index), store=None)
        return json.loads(provider.tools[0].tool.func(experiment_ids=[BASE, ATTENTION], runtime=runtime)), context
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, range(2)))
    assert results[0][0] == results[1][0] == invoke(2)[0]
    result = results[0][0]
    assert len(list((output / str(team) / str(run_id)).iterdir())) == 1
    image = output / result["data"]["artifacts"][0]["path"]
    image.write_bytes(b"damaged")
    with pytest.raises(RecoverableToolError) as exc:
        invoke(3)
    assert exc.value.code == "EXPERIMENT_PLOT_FAILED" and image.read_bytes() == b"damaged"
