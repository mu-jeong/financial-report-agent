import sqlite3

import pytest

from src.core import conversation_store


def test_conversation_store_persists_threads_messages_and_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))

    thread_id = conversation_store.create_thread("테스트")
    user_message_id = conversation_store.append_message(thread_id, "user", "첫 질문")
    assistant_message_id = conversation_store.append_message(
        thread_id,
        "assistant",
        "첫 답변",
        {"rerank_info": [{"rank": 1, "file_name": "a.pdf"}]},
    )

    threads = conversation_store.list_threads()
    messages = conversation_store.list_messages(thread_id)
    chat_history = conversation_store.get_chat_history(thread_id)

    assert threads[0]["id"] == thread_id
    assert messages[0]["id"] == user_message_id
    assert messages[1]["id"] == assistant_message_id
    assert messages[0]["content"] == "첫 질문"
    assert messages[1]["metadata"]["rerank_info"][0]["file_name"] == "a.pdf"
    assert chat_history == [("사용자", "첫 질문"), ("AI", "첫 답변")]


def test_conversation_store_updates_message_and_excludes_running_history(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))

    thread_id = conversation_store.create_thread("테스트")
    conversation_store.append_message(thread_id, "user", "첫 질문")
    pending_id = conversation_store.append_message(
        thread_id,
        "assistant",
        "처리 중",
        {"status": "running"},
    )

    assert conversation_store.get_chat_history(thread_id) == [("사용자", "첫 질문")]

    conversation_store.update_message(
        pending_id,
        "첫 답변",
        {"status": "succeeded", "rerank_info": [{"rank": 1}]},
    )

    messages = conversation_store.list_messages(thread_id)
    assert messages[1]["content"] == "첫 답변"
    assert messages[1]["metadata"]["status"] == "succeeded"
    assert conversation_store.get_chat_history(thread_id) == [("사용자", "첫 질문"), ("AI", "첫 답변")]


def test_pending_exchange_is_atomic_if_assistant_insert_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))
    thread_id = conversation_store.create_thread("테스트")
    with conversation_store.get_connection() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_assistant_message
            BEFORE INSERT ON conversation_messages
            WHEN NEW.role = 'assistant'
            BEGIN
                SELECT RAISE(ABORT, 'assistant insert rejected');
            END
            """
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="assistant insert rejected"):
        conversation_store.append_pending_exchange(
            thread_id,
            "질문",
            "처리 중",
            {"status": "running"},
        )

    assert conversation_store.list_messages(thread_id) == []


def test_conversation_store_marks_interrupted_running_messages_failed(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))

    thread_id = conversation_store.create_thread("테스트")
    conversation_store.append_message(thread_id, "user", "질문")
    running_id = conversation_store.append_message(
        thread_id,
        "assistant",
        "AI가 리포트 내용을 검색하고 분석 중입니다...",
        {"status": "running", "job_id": "stale-job"},
    )
    succeeded_id = conversation_store.append_message(
        thread_id,
        "assistant",
        "완료된 답변",
        {"status": "succeeded", "job_id": "done-job"},
    )

    repaired_count = conversation_store.mark_interrupted_running_messages_failed()

    messages = conversation_store.list_messages(thread_id)
    repaired_message = next(message for message in messages if message["id"] == running_id)
    succeeded_message = next(message for message in messages if message["id"] == succeeded_id)
    assert repaired_count == 1
    assert repaired_message["metadata"]["status"] == "failed"
    assert repaired_message["metadata"]["error"] == "interrupted_background_job"
    assert "다시 질문해 주세요" in repaired_message["content"]
    assert repaired_message["metadata"]["job_id"] == "stale-job"
    assert succeeded_message["metadata"]["status"] == "succeeded"
    assert conversation_store.get_chat_history(thread_id) == [("사용자", "질문"), ("AI", "완료된 답변")]


def test_conversation_store_keeps_active_running_messages(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))

    thread_id = conversation_store.create_thread("테스트")
    running_id = conversation_store.append_message(
        thread_id,
        "assistant",
        "처리 중",
        {"status": "running", "job_id": "active-job"},
    )

    repaired_count = conversation_store.mark_interrupted_running_messages_failed(
        active_job_ids={"active-job"}
    )

    messages = conversation_store.list_messages(thread_id)
    running_message = next(message for message in messages if message["id"] == running_id)
    assert repaired_count == 0
    assert running_message["content"] == "처리 중"
    assert running_message["metadata"]["status"] == "running"


def test_conversation_store_delete_thread_clears_messages(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))

    thread_id = conversation_store.create_thread("테스트")
    conversation_store.append_message(thread_id, "user", "질문")
    conversation_store.delete_thread(thread_id)

    assert conversation_store.list_threads() == []
    assert conversation_store.list_messages(thread_id) == []


def test_conversation_store_repairs_legacy_question_mark_thread_name(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))

    thread_id = conversation_store.create_thread("??? ??")

    assert conversation_store.list_threads()[0]["id"] == thread_id
    assert conversation_store.list_threads()[0]["name"] == "새로운 대화"

def test_conversation_store_pins_threads_before_recent_unpinned_threads(tmp_path, monkeypatch):
    db_path = tmp_path / "conversations.db"
    monkeypatch.setattr(conversation_store, "CONVERSATION_DB_PATH", str(db_path))

    pinned_id = conversation_store.create_thread("고정 대화")
    recent_id = conversation_store.create_thread("최근 대화")
    conversation_store.set_thread_pinned(pinned_id, True)

    threads = conversation_store.list_threads()

    assert [thread["id"] for thread in threads] == [pinned_id, recent_id]
    assert threads[0]["pinned"] is True
    assert threads[1]["pinned"] is False

    conversation_store.set_thread_pinned(pinned_id, False)

    assert all(thread["pinned"] is False for thread in conversation_store.list_threads())

