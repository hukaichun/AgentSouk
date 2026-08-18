from __future__ import annotations

from souk.agui import rewrite_message_ids


def test_rewrite_message_ids_reuses_souk_id_across_events_for_same_original():
    id_map: dict[str, str] = {}
    start = rewrite_message_ids({"type": "TEXT_MESSAGE_START", "messageId": "uuid-1"}, id_map)
    content = rewrite_message_ids({"type": "TEXT_MESSAGE_CONTENT", "messageId": "uuid-1", "delta": "hi"}, id_map)
    end = rewrite_message_ids({"type": "TEXT_MESSAGE_END", "messageId": "uuid-1"}, id_map)

    assert start["messageId"].startswith("msg_")
    assert start["messageId"] == content["messageId"] == end["messageId"]
    assert start["messageId"] != "uuid-1"


def test_rewrite_message_ids_distinguishes_different_originals():
    id_map: dict[str, str] = {}
    a = rewrite_message_ids({"type": "TEXT_MESSAGE_START", "messageId": "uuid-a"}, id_map)
    b = rewrite_message_ids({"type": "TEXT_MESSAGE_START", "messageId": "uuid-b"}, id_map)
    assert a["messageId"] != b["messageId"]


def test_rewrite_message_ids_ignores_unrelated_event_types():
    event = {"type": "RUN_STARTED", "threadId": "thread_x"}
    assert rewrite_message_ids(event, {}) is event
