import hashlib
import json
import logging
import os
from typing import Annotated, Literal, TypedDict
from uuid import UUID, uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, ModelResponse, wrap_model_call
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from agent_service.checkpoints import with_agent_search_path
from agent_service.runtime import BudgetExhausted, fail_run, publish_result, reserve_model, settle_model
from agent_service.tooling import FatalToolError, ToolExecutionContext, budgeted_tool, load_tools
from db.connection import connect


log = logging.getLogger(__name__)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanStep(Strict):
    goal: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)


class Plan(Strict):
    steps: list[PlanStep] = Field(min_length=1, max_length=5)


class InvalidPlanError(ValueError):
    pass


class DraftClaim(Strict):
    text: str = Field(min_length=1)
    support: Literal["evidence", "unverified"]
    evidence_ids: list[str]
    reason: str | None = None


class Notice(Strict):
    code: str
    message: str


class FinalDraft(Strict):
    answer: str = Field(min_length=1)
    claims: list[DraftClaim] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)


ExecutionContext = ToolExecutionContext


def model() -> ChatOpenAI:
    base_url = os.environ.get("AGENT_MODEL_URL")
    api_key = os.environ.get("AGENT_MODEL_KEY", "local")
    name = os.environ.get("AGENT_MODEL", "")
    if not base_url or not name:
        raise RuntimeError("AGENT_MODEL_URL and AGENT_MODEL are required")
    normalized = base_url.rstrip("/")
    return ChatOpenAI(model=name, base_url=normalized if normalized.endswith("/v1") else normalized + "/v1", api_key=api_key, timeout=None, max_retries=0, temperature=0)


@wrap_model_call
def budgeted_model(request: ModelRequest, handler) -> ModelResponse:
    context: ExecutionContext = request.runtime.context
    call_id = reserve_model(
        context.run_id,
        context.lease_token,
        context.purpose,
        os.environ.get("AGENT_MODEL", "unknown"),
        context.step_id,
        input_summary={"message_count": len(request.state.get("messages", []))},
    )
    try:
        response = handler(request)
        usage = next((message.usage_metadata for message in response.result if isinstance(message, AIMessage) and message.usage_metadata), {}) or {}
        if not settle_model(context.run_id, context.lease_token, call_id, "succeeded", input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), output_summary={"message_count": len(response.result)}):
            raise PermissionError("execution lease was revoked")
        context.triggering_model_call_id = call_id
        return response
    except (BudgetExhausted, PermissionError):
        raise
    except Exception as exc:
        settle_model(context.run_id, context.lease_token, call_id, "failed", error_code="MODEL_CALL_FAILED", output_summary={"exception_type": type(exc).__name__})
        raise


def react_agent(saver: PostgresSaver):
    return create_agent(
        model(),
        load_tools(),
        system_prompt="Choose from the available tools when they help complete the task. Treat tool outputs as observations and do not claim evidence support unless an evidence tool returned it. Return a concise execution summary; a separate node writes the final answer.",
        middleware=[budgeted_model, budgeted_tool],
        context_schema=ExecutionContext,
        checkpointer=saver,
    )


def invoke_structured(llm, schema, messages, context: ExecutionContext, purpose: str, final: bool = False):
    call_id = reserve_model(context.run_id, context.lease_token, purpose, os.environ.get("AGENT_MODEL", "unknown"), context.step_id, final, {"message_count": len(messages), "schema": schema.__name__})
    try:
        result = llm.with_structured_output(schema).invoke(messages)
        if not settle_model(context.run_id, context.lease_token, call_id, "succeeded", output_summary={"schema": schema.__name__}):
            raise PermissionError("execution lease was revoked")
        return result
    except (BudgetExhausted, PermissionError):
        raise
    except Exception as exc:
        settle_model(context.run_id, context.lease_token, call_id, "failed", error_code="MODEL_CALL_FAILED", output_summary={"exception_type": type(exc).__name__})
        raise


def collect_evidence(messages, target: dict[str, dict]) -> None:
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(message.content)
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload.get("evidences", [])
        if isinstance(payload.get("evidence_id"), str):
            items = [payload]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
                target[item["evidence_id"]] = item


def final_generate(task: str, summaries: list[str], context: ExecutionContext, partial: bool) -> dict:
    evidence = list(context.evidence.values())
    prompt = {
        "task": task,
        "execution_summaries": summaries,
        "available_evidence": evidence,
        "required": "Return a final answer. Evidence-supported claims may cite only available evidence_id values. Unverified claims need a reason. Do not invent citations.",
        "partial": partial,
    }
    draft = invoke_structured(model(), FinalDraft, [("system", "Produce the final user-facing result with verifiable citations."), ("user", json.dumps(prompt, ensure_ascii=False))], context, "final", final=True).model_dump()
    draft["notices"] = list({item["code"]: item for item in [*context.notices, *draft.get("notices", [])]}.values())
    cited = list(dict.fromkeys(eid for claim in draft["claims"] for eid in claim["evidence_ids"]))
    draft["citations"] = [context.evidence[eid] for eid in cited if eid in context.evidence]
    return draft


class PlanState(TypedDict):
    task: str
    steps: list[dict]
    index: int
    summaries: Annotated[list[str], lambda left, right: left + right]
    evidence: dict[str, dict]
    notices: list[dict]


def run_plan(run_id: UUID, token: UUID, team_id: UUID, key_id: str, task: str, saver: PostgresSaver) -> tuple[list[str], dict[str, dict], list[dict], bool]:
    llm = model()
    agent = react_agent(saver)

    def plan_node(state: PlanState):
        context = ExecutionContext(run_id, token, team_id=team_id, key_id=key_id, purpose="planner")
        try:
            plan = invoke_structured(llm, Plan, [("system", "Create a fixed plan of 1 to 5 ordered steps. Each step needs a goal and observable completion condition. Do not prescribe tool names."), ("user", state["task"])], context, "planner")
        except ValueError as exc:
            raise InvalidPlanError from exc
        steps = [item.model_dump() for item in plan.steps]
        with connect("AGENT_DATABASE_URL") as conn:
            for ordinal, step in enumerate(steps, 1):
                conn.execute("INSERT INTO agent.run_steps(id,run_id,ordinal,goal,completion_condition,status) VALUES (%s,%s,%s,%s,%s,'pending') ON CONFLICT (run_id,ordinal) DO NOTHING", (uuid4(), run_id, ordinal, step["goal"], step["completion_condition"]))
        return {"steps": steps, "index": 0}

    def execute_node(state: PlanState):
        index = state["index"]
        step = state["steps"][index]
        with connect("AGENT_DATABASE_URL") as conn:
            row = conn.execute("UPDATE agent.run_steps SET status='running',started_at=COALESCE(started_at,now()) WHERE run_id=%s AND ordinal=%s RETURNING id", (run_id, index + 1)).fetchone()
        context = ExecutionContext(
            run_id,
            token,
            team_id=team_id,
            key_id=key_id,
            step_id=row["id"],
            purpose="react_step",
            evidence=dict(state.get("evidence", {})),
            notices=list(state.get("notices", [])),
        )
        prompt = f"Original task: {state['task']}\nCurrent step goal: {step['goal']}\nCompletion condition: {step['completion_condition']}"
        config = {"configurable": {"thread_id": f"{run_id}:step:{index + 1}"}, "recursion_limit": 64}
        snapshot = agent.get_state(config)
        output = agent.invoke(None if snapshot.values else {"messages": [{"role": "user", "content": prompt}]}, config=config, context=context)
        collect_evidence(output["messages"], context.evidence)
        summary = str(output["messages"][-1].content)
        safe_summary = f"sha256={hashlib.sha256(summary.encode()).hexdigest()}; characters={len(summary)}"
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("UPDATE agent.run_steps SET status='completed',result_summary=%s,finished_at=now() WHERE id=%s", (safe_summary, row["id"]))
        return {"index": index + 1, "summaries": [summary], "evidence": context.evidence, "notices": context.notices}

    graph = StateGraph(PlanState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "execute")
    graph.add_conditional_edges("execute", lambda state: "execute" if state["index"] < len(state["steps"]) else END, {"execute": "execute", END: END})
    compiled = graph.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 64}
    snapshot = compiled.get_state(config)
    result = compiled.invoke(None if snapshot.values else {"task": task, "steps": [], "index": 0, "summaries": [], "evidence": {}, "notices": []}, config=config)
    return result["summaries"], result["evidence"], result["notices"], False


def execute_run(run_id: UUID, token: UUID) -> None:
    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("SELECT task,mode,team_id,key_id FROM agent.agent_runs WHERE id=%s AND lease_token=%s AND status='running'", (run_id, token)).fetchone()
    if not run:
        return
    try:
        dsn = with_agent_search_path(os.environ["AGENT_DATABASE_URL"])
        with PostgresSaver.from_conn_string(dsn) as saver:
            if run["mode"] == "react":
                context = ExecutionContext(run_id, token, team_id=run["team_id"], key_id=run["key_id"])
                agent = react_agent(saver)
                config = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 64}
                snapshot = agent.get_state(config)
                output = agent.invoke(None if snapshot.values else {"messages": [{"role": "user", "content": run["task"]}]}, config=config, context=context)
                collect_evidence(output["messages"], context.evidence)
                summaries = [str(output["messages"][-1].content)]
                partial = False
            else:
                summaries, evidence, notices, partial = run_plan(run_id, token, run["team_id"], run["key_id"], run["task"], saver)
                context = ExecutionContext(run_id, token, team_id=run["team_id"], key_id=run["key_id"], evidence=evidence, notices=notices)
            draft = final_generate(run["task"], summaries, context, partial)
            publish_result(run_id, token, draft, context.evidence, partial, "budget_exhausted" if partial else "model_finished")
    except BudgetExhausted:
        try:
            context = locals().get("context") or ExecutionContext(run_id, token, team_id=run["team_id"], key_id=run["key_id"])
            draft = final_generate(run["task"], locals().get("summaries", []), context, True)
            publish_result(run_id, token, draft, context.evidence, True, "budget_exhausted")
        except Exception:
            fail_run(run_id, token, "QUOTA_EXCEEDED", "budget_exhausted_without_result")
    except InvalidPlanError as exc:
        fail_run(run_id, token, "INVALID_PLAN", type(exc).__name__)
    except ValueError as exc:
        fail_run(run_id, token, "MODEL_CALL_FAILED", type(exc).__name__)
    except FatalToolError as exc:
        fail_run(run_id, token, exc.run_code)
    except (PermissionError,):
        return
    except Exception:
        log.exception("Agent run execution failed", extra={"run_id": str(run_id)})
        fail_run(run_id, token, "MODEL_CALL_FAILED")
