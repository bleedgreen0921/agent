"""Drive one synthetic document through the running RAG and Agent services."""

import argparse
import json
import os
import time

import httpx


TERMINAL = {"completed", "partial", "failed", "cancelled"}


def wait(client: httpx.Client, url: str, headers: dict[str, str], terminal: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if payload["status"] in terminal:
            return payload
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rag-url", default=os.environ.get("RAG_PUBLIC_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--agent-url", default=os.environ.get("AGENT_PUBLIC_URL", "http://127.0.0.1:8002"))
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY"))
    parser.add_argument("--mode", choices=("react", "plan_execute"), default="react")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if not args.admin_key:
        parser.error("--admin-key or ADMIN_KEY is required")

    admin_headers = {"Authorization": "Bearer " + args.admin_key}
    with httpx.Client(timeout=10) as client:
        team_response = client.post(args.rag_url + "/v1/admin/teams", headers=admin_headers, json={"name": "synthetic-demo-" + str(time.time_ns())})
        team_response.raise_for_status()
        team_id = team_response.json()["team_id"]
        key_response = client.post(args.rag_url + f"/v1/admin/teams/{team_id}/keys", headers=admin_headers, json={})
        key_response.raise_for_status()
        team_headers = {"Authorization": "Bearer " + key_response.json()["key"]}

        content = b"Synthetic records policy\nResearch records must be retained for seven years after project completion.\n"
        upload = client.post(
            args.rag_url + "/v1/admin/documents",
            headers={**admin_headers, "Idempotency-Key": "synthetic-policy-" + team_id},
            data={"title": "Synthetic Records Policy", "visibility": "restricted", "team_ids": json.dumps([team_id])},
            files={"file": ("policy.txt", content, "text/plain")},
        )
        upload.raise_for_status()
        document = upload.json()
        version = wait(
            client,
            args.rag_url + f"/v1/admin/documents/{document['document_id']}/versions/{document['document_version_id']}",
            admin_headers,
            {"active", "failed"},
            args.timeout,
        )
        if version["status"] != "active":
            raise RuntimeError("Document processing failed: " + json.dumps(version))

        submitted = client.post(
            args.agent_url + "/v1/runs",
            headers={**team_headers, "Idempotency-Key": "synthetic-run-" + args.mode},
            json={"task": "According to the private policy, how long must research records be retained? Cite the evidence.", "mode": args.mode},
        )
        submitted.raise_for_status()
        result = wait(client, args.agent_url + "/v1/runs/" + submitted.json()["run_id"], team_headers, TERMINAL, args.timeout)
        if result["status"] not in {"completed", "partial"}:
            raise RuntimeError("Agent run failed: " + json.dumps(result))
        citations = result.get("result", {}).get("citations", [])
        if not citations or "seven years" not in citations[0]["content"]:
            raise RuntimeError("Agent result did not preserve the expected citation")
        print(json.dumps({"team_id": team_id, "document": document, "run": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
