"""Deterministic HTTP model stubs for the synthetic demo only."""

import hashlib
import json
import re
import time
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict


app = FastAPI(title="Synthetic demo model service")


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="allow")


def completion(message: dict, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-" + uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "synthetic-demo",
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


def schema_name(body: dict) -> str | None:
    response_format = body.get("response_format") or {}
    json_schema = response_format.get("json_schema") or {}
    if json_schema.get("name"):
        return json_schema["name"]
    names = [item.get("function", {}).get("name") for item in body.get("tools", [])]
    for name in ("Plan", "FinalDraft"):
        if name in names:
            return name
    return None


def evidence_id(body: dict) -> str | None:
    raw = json.dumps(body.get("messages", []), ensure_ascii=False)
    match = re.search(r'ev_[0-9a-fA-F-]{36}', raw)
    return match.group(0) if match else None


def structured_value(name: str, body: dict) -> dict:
    if name == "Plan":
        return {"steps": [{"goal": "Find the retention period in the private policy", "completion_condition": "A policy fragment states a retention period"}]}
    found = evidence_id(body)
    if found:
        return {
            "answer": "The synthetic policy states that research records are retained for seven years.",
            "claims": [{"text": "Research records are retained for seven years.", "support": "evidence", "evidence_ids": [found], "reason": None}],
            "notices": [],
        }
    return {
        "answer": "The synthetic run completed without knowledge-base evidence.",
        "claims": [{"text": "No evidence was available.", "support": "unverified", "evidence_ids": [], "reason": "The run received no evidence"}],
        "notices": [],
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "synthetic-models"}


@app.post("/v1/embeddings")
def embeddings(body: OpenBody):
    payload = body.model_dump()
    values = payload.get("input", [])
    if isinstance(values, str):
        values = [values]
    # Every non-empty text points in the same direction. This is intentional: the
    # demo validates service integration and evidence flow, not retrieval quality.
    vector = [1.0] + [0.0] * 1023
    return {"object": "list", "model": payload.get("model", "synthetic"), "data": [{"object": "embedding", "index": index, "embedding": vector} for index, _ in enumerate(values)], "usage": {"prompt_tokens": len(values), "total_tokens": len(values)}}


@app.post("/rerank")
def rerank(body: OpenBody):
    documents = body.model_dump().get("documents", [])
    return {"results": [{"index": index, "relevance_score": 1.0 - index / max(len(documents), 1)} for index in range(len(documents))]}


@app.post("/v1/chat/completions")
def chat(body: OpenBody):
    payload = body.model_dump()
    name = schema_name(payload)
    if name:
        value = structured_value(name, payload)
        tool_names = [item.get("function", {}).get("name") for item in payload.get("tools", [])]
        if name in tool_names:
            call = {"id": "call_" + uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(value)}}
            return completion({"content": None, "tool_calls": [call]}, "tool_calls")
        return completion({"content": json.dumps(value)})

    tools = [item.get("function", {}).get("name") for item in payload.get("tools", [])]
    messages = payload.get("messages", [])
    if "search_evidence" in tools:
        if any(message.get("role") == "tool" for message in messages):
            return completion({"content": "Evidence was retrieved and is ready for final synthesis."})
        arguments = json.dumps({"query": "research records retention period", "top_k": 5})
        call = {"id": "call_" + uuid4().hex, "type": "function", "function": {"name": "search_evidence", "arguments": arguments}}
        return completion({"content": None, "tool_calls": [call]}, "tool_calls")

    # RAG query rewriting sends no tool schema.
    content = next((str(message.get("content", "")) for message in reversed(messages) if message.get("role") == "user"), "")
    return completion({"content": content.strip() or hashlib.sha256(b"synthetic").hexdigest()})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8090)
