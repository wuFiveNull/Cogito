"""Read Message aggregates solely from Event streams and restricted payloads."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from cogito.infrastructure.payload_store import PayloadStore
from cogito.store.event_replay import replay_message
from cogito.store.event_store import EventStore


class EventMessageReader:
    """Event-sourced Message read model used by runtime consumers.

    The Event keeps searchable safe metadata; the complete message envelope is
    retrieved only through ``PayloadStore`` after the caller has already been
    authorized to use message content.
    """

    def __init__(self, conn: sqlite3.Connection, payload_store: PayloadStore) -> None:
        self._conn = conn
        self._payload_store = payload_store

    def get(self, message_id: str) -> dict[str, Any] | None:
        stream = EventStore(self._conn).read_stream("message", message_id)
        projection = replay_message(stream, message_id)
        if projection is None:
            return None
        recorded = next(
            (event for event in reversed(stream) if event.event_type == "interaction.message.recorded"),
            None,
        )
        if recorded is None or not recorded.payload_ref:
            return None
        try:
            raw = self._payload_store.get(recorded.payload_ref)
            if raw is None:
                return None
            envelope = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None
        message = envelope.get("message")
        if not isinstance(message, dict) or message.get("message_id") != message_id:
            return None
        return message

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return self._list_matching(lambda state: state.session_id == session_id)

    def list_for_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._list_matching(lambda state: state.conversation_id == conversation_id)

    def _list_matching(self, predicate: Any) -> list[dict[str, Any]]:
        events = EventStore(self._conn).read_stream_type("message")
        states = {
            message_id: replay_message(events, message_id)
            for message_id in {event.stream_id for event in events}
        }
        rows: list[dict[str, Any]] = []
        for message_id, state in states.items():
            if state is None or not predicate(state):
                continue
            message = self.get(message_id)
            if message is None:
                continue
            rows.append(message)
        return sorted(rows, key=lambda item: (int(item.get("receive_sequence", 0)), item["message_id"]))

    def find_receive_sequence(self, message_id: str) -> int:
        stream = EventStore(self._conn).read_stream("message", message_id)
        state = replay_message(stream, message_id)
        return state.receive_sequence if state is not None else 0
