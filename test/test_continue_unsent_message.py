"""Resume on a turn whose message never reached the agent re-sends that message.

A ``memory_unavailable`` error ends a turn while its context is still being
prepared, so the agent's session never holds the user's message. Resume there
must queue the message itself, not the generic resume body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import _MANUAL_RESUME_MSG, api_chat_slot_continue
from kiro_crew.dashboard.chat_utils import (
    SYNTHETIC_RECOVERY_KIND,
    RecoveryPayload,
    is_synthetic_payload_item,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history_schema import HUMAN_TURN_META_KEY
from kiro_crew.shared_crew.attribution import stamp_message_author, stamp_principal_author
from kiro_crew.shared_crew.contracts import ActorContext, Principal, Transport
from kiro_crew.shared_crew.identity import local_operator_principal

_POOL_BUSY = (
    "memory_unavailable: the memory pool was busy and this turn's context could "
    "not be prepared in time; nothing was sent -- try again in a moment"
)


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _mem_error() -> dict:
    return {"role": "error", "content": _POOL_BUSY, "meta": {"code": "memory_unavailable"}}


def _replay(text: str) -> dict:
    return {"role": "inject", "content": text, "meta": {"injectKind": "user_replay"}}


_OPERATOR = ActorContext(
    principal=local_operator_principal("owner-subject"), request_id="r1", transport=Transport.HTTP
)


def _composer_meta(**extra) -> dict:
    """The provenance a person's own dashboard send stamps on its row."""
    return stamp_message_author({HUMAN_TURN_META_KEY: True, **extra}, _OPERATOR)


def unsent_user_message(rows: list[dict]) -> str | None:
    row = chat_handlers.unsent_user_row(rows)
    return None if row is None else row["content"]


class TestUnsentUserMessage:
    def test_user_then_pool_busy_error_returns_the_message(self):
        assert (
            unsent_user_message([_user("old"), _user("leave it on"), _mem_error()]) == "leave it on"
        )

    def test_second_failure_after_a_resend_still_returns_the_message(self):
        rows = [_user("leave it on"), _mem_error(), _replay("leave it on"), _mem_error()]
        assert unsent_user_message(rows) == "leave it on"

    def test_a_reply_after_the_message_means_it_was_received(self):
        rows = [_user("do it"), {"role": "assistant", "content": "working"}, _mem_error()]
        assert unsent_user_message(rows) is None

    def test_a_tool_row_after_the_message_means_it_was_received(self):
        rows = [_user("do it"), {"role": "tool", "content": "ran"}, _mem_error()]
        assert unsent_user_message(rows) is None

    def test_another_failure_kind_keeps_the_generic_resume(self):
        rows = [_user("do it"), {"role": "error", "content": "connection lost", "meta": {}}]
        assert unsent_user_message(rows) is None

    def test_a_resend_still_in_flight_is_not_matched(self):
        assert unsent_user_message([_user("x"), _mem_error(), _replay("x")]) is None

    def test_a_replay_with_no_error_before_it_is_not_matched(self):
        assert unsent_user_message([_user("x"), _replay("x"), _mem_error()]) is None

    def test_the_turn_end_marker_after_the_error_is_walked_past(self):
        rows = [_user("leave it on"), _mem_error(), {"role": "done", "content": "", "cls": "done"}]
        assert unsent_user_message(rows) == "leave it on"

    def test_a_streamed_chunk_after_the_message_means_it_was_received(self):
        rows = [_user("do it"), {"role": "chunk", "content": "work"}, _mem_error()]
        assert unsent_user_message(rows) is None

    def test_no_user_row_returns_none(self):
        assert unsent_user_message([_mem_error()]) is None


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return app


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.broadcast_ws = MagicMock()
    state.subagents = None
    return state


@pytest.fixture
def _patched():
    mock_sel = MagicMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
        patch(
            "kiro_crew.dashboard.chat_handlers._start_next_queued_turn",
            AsyncMock(return_value=True),
        ),
    ):
        yield


async def _press_resume(slot: _ChatSlot) -> int:
    async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
        resp = await client.post("/api/chat/slots/s/continue")
        return resp.status


class TestResumeAfterPoolBusy:
    @pytest.mark.asyncio
    async def test_resume_queues_the_unsent_message_as_the_users_words(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u", meta=_composer_meta())
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})

        assert await _press_resume(slot) == 200

        entry = slot._queue[0]
        assert entry["content"] == "Leave the watchers on"
        assert entry["kind"] == SYNTHETIC_RECOVERY_KIND
        assert entry["payload"] == RecoveryPayload.ORIGINAL
        assert is_synthetic_payload_item(entry) is False

    @pytest.mark.asyncio
    async def test_the_resend_carries_the_attachments_of_the_unsent_message(self, _patched):
        slot = _ChatSlot("s")
        slot.append(
            "user",
            "Review these",
            "msg msg-u",
            meta=_composer_meta(files=["/w/a.png"], dirs=["/w/src"]),
        )
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})

        assert await _press_resume(slot) == 200

        meta = slot._queue[0]["meta"]
        assert meta["files"] == ["/w/a.png"]
        assert meta["dirs"] == ["/w/src"]

    @pytest.mark.asyncio
    async def test_resume_resends_on_the_live_tail_a_finished_turn_leaves(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u", meta=_composer_meta())
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})
        slot.append("done", "", "done")

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == "Leave the watchers on"
        assert slot._queue[0]["payload"] == RecoveryPayload.ORIGINAL

    @pytest.mark.asyncio
    @pytest.mark.parametrize("link", ["linked_session_key", "_slack_linked"])
    async def test_a_channel_linked_slot_keeps_the_generic_resume_body(self, _patched, link):
        slot = _ChatSlot("s")
        setattr(slot, link, "slack:1700000000.000100" if link == "linked_session_key" else True)
        slot.append("user", "Leave the watchers on", "msg msg-u", meta=_composer_meta())
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert is_synthetic_payload_item(slot._queue[0]) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("probe", [True, RuntimeError("store unreadable")])
    async def test_a_mirrored_or_unreadable_slot_keeps_the_generic_resume_body(
        self, _patched, probe
    ):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u", meta=_composer_meta())
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})

        mirror = (
            MagicMock(side_effect=probe)
            if isinstance(probe, Exception)
            else MagicMock(return_value=probe)
        )
        with patch("kiro_crew.dashboard.session_control._has_channel_mirror", mirror):
            assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        mirror.assert_called()

    @pytest.mark.asyncio
    async def test_resume_after_a_reply_keeps_the_generic_resume_body(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u", meta=_composer_meta())
        slot.append("assistant", "Working on it", "msg msg-a")
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert is_synthetic_payload_item(slot._queue[0]) is True


def _channel_meta() -> dict:
    return stamp_principal_author({HUMAN_TURN_META_KEY: True}, Principal.unknown())


def _operator_without_human_mark() -> dict:
    return stamp_message_author({}, _OPERATOR)


class TestResumeResendsOnlyThePersonsOwnSend:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "meta",
        [
            pytest.param(None, id="no-meta"),
            pytest.param(_operator_without_human_mark(), id="author-without-human-mark"),
            pytest.param(_channel_meta(), id="channel-row-unknown-author"),
            pytest.param(
                _composer_meta(relay={"senderKey": "dashboard:peer", "verb": "send"}),
                id="peer-relay",
            ),
            pytest.param(_composer_meta(turnActor="app"), id="app-turn-actor"),
            pytest.param(_composer_meta(commands_off=True), id="commands-off"),
            pytest.param(
                _composer_meta(channel_recipient={"channel_type": "discord", "id": "c1"}),
                id="channel-recipient",
            ),
            pytest.param(_composer_meta(send_origin_slot="peer"), id="send-origin"),
            pytest.param(_composer_meta(slack_sender={"user": "U1"}), id="slack-sender"),
            pytest.param(_composer_meta(cardOrigin=True), id="card-origin"),
            pytest.param(_composer_meta(turnProvenanceRestored=True), id="restored-queue"),
        ],
    )
    async def test_a_row_not_provably_the_persons_own_keeps_the_generic_resume_body(
        self, _patched, meta
    ):
        slot = _ChatSlot("s")
        slot.append("user", "/clear", "msg msg-u", meta=meta)
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert is_synthetic_payload_item(slot._queue[0]) is True
