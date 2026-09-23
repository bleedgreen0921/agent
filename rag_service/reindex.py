"""Shadow embedding revision build and atomic global switch."""

import argparse
from uuid import UUID, uuid4

from db.connection import connect
from rag_service.chunking import ChunkDraft, embedding_input
from rag_service.models import embed
from rag_service.worker import vector_literal


def start(model: str, dimensions: int) -> UUID:
    if not model or not 1 <= dimensions <= 4096:
        raise ValueError("invalid revision configuration")
    revision_id = uuid4()
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE")
        if conn.execute("SELECT 1 FROM rag.index_revisions WHERE state='building'").fetchone():
            raise ValueError("shadow revision already building")
        conn.execute("""INSERT INTO rag.index_revisions(id,model,dimensions,document_template,query_template,state)
            VALUES (%s,%s,%s,'title-content-v1','qwen-retrieval-v2','building')""", (revision_id, model, dimensions))
    return revision_id


def build(revision_id: UUID, batch_size: int = 16) -> int:
    completed = 0
    while True:
        with connect("RAG_DATABASE_URL") as conn:
            revision = conn.execute("SELECT id,model,dimensions FROM rag.index_revisions WHERE id=%s AND state='building'", (revision_id,)).fetchone()
            if not revision:
                raise ValueError("revision is not building")
            rows = conn.execute("""SELECT c.id,c.document_id,c.version_id,c.content,c.heading_path,d.title
                FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id
                WHERE d.deleted_at IS NULL AND d.active_version_id=c.version_id
                  AND NOT EXISTS (SELECT 1 FROM rag.embeddings e WHERE e.chunk_id=c.id AND e.revision_id=%s)
                ORDER BY c.id LIMIT %s""", (revision_id, batch_size)).fetchall()
        if not rows:
            return completed
        drafts = [ChunkDraft(row["content"], tuple(row["heading_path"]), {}) for row in rows]
        vectors = embed([embedding_input(row["title"], draft) for row, draft in zip(rows, drafts)], revision["model"], revision["dimensions"])
        with connect("RAG_DATABASE_URL") as conn:
            conn.execute("SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE")
            if not conn.execute("SELECT 1 FROM rag.index_revisions WHERE id=%s AND state='building'", (revision_id,)).fetchone():
                raise ValueError("revision changed during build")
            for row, vector in zip(rows, vectors):
                doc = conn.execute("SELECT active_version_id,deleted_at FROM rag.documents WHERE id=%s FOR UPDATE", (row["document_id"],)).fetchone()
                if doc["deleted_at"] or doc["active_version_id"] != row["version_id"]:
                    continue
                conn.execute("""INSERT INTO rag.embeddings(chunk_id,revision_id,embedding)
                    VALUES (%s,%s,%s::vector) ON CONFLICT DO NOTHING""", (row["id"], revision_id, vector_literal(vector)))
                completed += 1


def switch(revision_id: UUID) -> None:
    with connect("RAG_DATABASE_URL") as conn:
        state = conn.execute("SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE").fetchone()
        if not conn.execute("SELECT 1 FROM rag.index_revisions WHERE id=%s AND state='building'", (revision_id,)).fetchone():
            raise ValueError("revision is not building")
        missing = conn.execute("""SELECT count(*) AS n FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id
            WHERE d.deleted_at IS NULL AND d.active_version_id=c.version_id
              AND NOT EXISTS (SELECT 1 FROM rag.embeddings e WHERE e.chunk_id=c.id AND e.revision_id=%s)""", (revision_id,)).fetchone()["n"]
        if missing:
            raise ValueError(f"shadow revision missing {missing} active chunks")
        conn.execute("UPDATE rag.index_revisions SET state='superseded' WHERE id=%s", (state["active_revision_id"],))
        conn.execute("UPDATE rag.index_revisions SET state='active' WHERE id=%s", (revision_id,))
        conn.execute("UPDATE rag.index_state SET active_revision_id=%s WHERE singleton=true", (revision_id,))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start")
    begin.add_argument("--model", required=True)
    begin.add_argument("--dimensions", type=int, required=True)
    for name in ("build", "switch"):
        command = commands.add_parser(name)
        command.add_argument("revision_id", type=UUID)
    args = parser.parse_args()
    if args.command == "start":
        print(start(args.model, args.dimensions))
    elif args.command == "build":
        print(build(args.revision_id))
    else:
        switch(args.revision_id)


if __name__ == "__main__":
    main()
