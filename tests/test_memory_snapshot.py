from uuid import uuid4

from agent_service import memory


def item(ordinal, user, assistant=None):
    return {"turn_id": str(uuid4()), "ordinal": ordinal, "user": user,
            "assistant": assistant, "status": "completed", "created_at": "2026-01-01T00:00:00+00:00"}


def test_oversized_recent_user_turn_is_retained():
    turn = item(1, "start " + "x" * 9000 + " end", "Acknowledged")
    summary, history = memory._budgeted_context({"content": "older summary"}, [turn])

    assert summary == ""
    assert len(history) == 1
    assert history[0]["turn_id"] == turn["turn_id"]
    assert history[0]["user"].startswith("start ")
    assert history[0]["user"].endswith(" end")
    assert "…" in history[0]["user"]
    assert history[0]["assistant"] == ""
    assert len(history[0]["user"]) == memory.HISTORY_CHARS


def test_partial_budget_keeps_recent_turn_and_truncates_older_turn():
    older = item(1, "first " + "x" * 7000 + " last", "older answer")
    recent = item(2, "r" * 3000)
    summary, history = memory._budgeted_context({"content": "summary"}, [older, recent])

    assert summary == ""
    assert [turn["ordinal"] for turn in history] == [1, 2]
    assert history[0]["user"].startswith("first ")
    assert history[0]["user"].endswith(" last")
    assert history[0]["assistant"] == ""
    assert sum(len(turn["user"]) + len(turn["assistant"] or "") for turn in history) == memory.HISTORY_CHARS


def test_oversized_assistant_preserves_user_and_both_ends_of_answer():
    turn = item(1, "question", "start " + "x" * 9000 + " end")
    _, history = memory._budgeted_context(None, [turn])

    assert history[0]["user"] == "question"
    assert history[0]["assistant"].startswith("start ")
    assert history[0]["assistant"].endswith(" end")
    assert len(history[0]["assistant"]) == memory.HISTORY_CHARS - len("question")


def test_snapshot_query_bounds_rows_and_text_and_respects_summary_boundary():
    class Cursor:
        def fetchall(self):
            return [{"ordinal": n} for n in range(104, 100, -1)]

    class Connection:
        def execute(self, sql, params):
            self.sql, self.params = sql, params
            return Cursor()

    conn = Connection()
    conversation_id = uuid4()
    rows = memory._snapshot_history(conn, conversation_id, before=105, covered=100)

    assert [row["ordinal"] for row in rows] == [101, 102, 103, 104]
    assert "ORDER BY ordinal DESC LIMIT %s" in conn.sql
    assert "char_length(t.user_text)>%s" in conn.sql
    assert "char_length(rr.answer)>%s" in conn.sql
    assert conn.params[:6] == (16000, 7999, 8000, 16000, 7999, 8000)
    assert conn.params[-4:] == (conversation_id, 100, 105, memory.SNAPSHOT_TURNS)

    memory._snapshot_history(conn, conversation_id, before=105, covered=0)
    assert conn.params[-4:] == (conversation_id, 0, 105, memory.SNAPSHOT_TURNS)
