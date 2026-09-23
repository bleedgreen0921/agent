import json

from fastapi.testclient import TestClient

from scripts.mock_model_service import app


def test_synthetic_model_http_shapes():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        embedding = client.post("/v1/embeddings", json={"model": "synthetic", "input": ["one", "two"]}).json()
        assert [item["index"] for item in embedding["data"]] == [0, 1]
        assert len(embedding["data"][0]["embedding"]) == 1024

        tool = client.post(
            "/v1/chat/completions",
            json={
                "model": "synthetic",
                "messages": [{"role": "user", "content": "Find the retention period"}],
                "tools": [{"type": "function", "function": {"name": "search_evidence"}}],
            },
        ).json()["choices"][0]
        assert tool["finish_reason"] == "tool_calls"
        arguments = json.loads(tool["message"]["tool_calls"][0]["function"]["arguments"])
        assert arguments == {"query": "research records retention period", "top_k": 5}

        plan = client.post(
            "/v1/chat/completions",
            json={"model": "synthetic", "messages": [], "response_format": {"type": "json_schema", "json_schema": {"name": "Plan"}}},
        ).json()["choices"][0]["message"]["content"]
        assert len(json.loads(plan)["steps"]) == 1

        reranked = client.post("/rerank", json={"documents": ["a", "b"]}).json()["results"]
        assert [item["index"] for item in reranked] == [0, 1]
