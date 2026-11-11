"""Per-tool "Always allow": the approval card's decision and the MCP tool list's routes.

Both write the identity-keyed ``hooks.auto_approve_tools`` entry ``@server/tool``
(``dashboard.always_allow_tool``). The card grants only the ref its own pending
row carries; the routes refuse a Kiro Crew server, a wildcard and a tool an
operator ``confirm_tools`` pattern covers.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import always_allow_tool
from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.handlers.security_tool_grants import (
    api_tool_grant_add,
    api_tool_grant_remove,
    api_tool_grants_list,
)
from kiro_crew.dashboard.state import SlotOrigin

REF = "@notes/lookup"


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def no_refusal(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: False
    )
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _dashboard_user(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _app_caller(request: web.Request, handler):
    request["app"] = "some-app"
    return await handler(request)


def _app(state, middleware=_dashboard_user) -> web.Application:
    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_get("/api/security/tool-grants", api_tool_grants_list)
    app.router.add_post("/api/security/tool-grants", api_tool_grant_add)
    app.router.add_post("/api/security/tool-grants/remove", api_tool_grant_remove)
    return app


def _pending(state, request_id: str, meta: dict):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", REF, json.dumps({"request_id": request_id, **meta}))
    slot.register_approval(request_id, fut, row)
    return slot, fut


def _decide(slot, request_id: str, **extra) -> dict:
    return {
        "action": "always_allow_tool",
        "request_id": request_id,
        "origin": "native",
        "request_mid": slot.approval_instance(request_id),
        **extra,
    }


def _stored(tmp_path) -> list:
    path = tmp_path / "config.json"
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("hooks", {}).get("auto_approve_tools", [])


GRANTABLE = {
    always_allow_tool.FIELD_GRANTABLE: "1",
    always_allow_tool.FIELD_REF: REF,
}


# -- the card's decision ----------------------------------------------------


@pytest.mark.asyncio
async def test_card_grant_writes_the_cards_ref_and_approves(state, tmp_path, no_refusal):
    slot, fut = _pending(state, "r1", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json=_decide(slot, "r1", pattern=REF)
        )
    assert resp.status == 200
    assert fut.result() == "approved"
    assert _stored(tmp_path) == [REF]


@pytest.mark.asyncio
async def test_card_grant_refuses_a_client_pattern_naming_another_tool(state, tmp_path, no_refusal):
    slot, fut = _pending(state, "r2", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json=_decide(slot, "r2", pattern="@notes/delete_all"),
        )
        body = await resp.json()
    assert resp.status == 409 and body["code"] == "approval_superseded"
    assert not fut.done()
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_card_without_the_servers_proof_grants_nothing(state, tmp_path, no_refusal):
    slot, fut = _pending(state, "r3", {always_allow_tool.FIELD_REF: REF})
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json=_decide(slot, "r3", pattern=REF)
        )
    assert resp.status == 400
    assert not fut.done()
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_card_grant_needs_the_native_target(state, tmp_path, no_refusal):
    _slot, fut = _pending(state, "r4", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": "always_allow_tool", "request_id": "r4", "pattern": REF},
        )
    assert resp.status == 403
    assert not fut.done()
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_card_grant_refused_when_confirm_tools_now_covers_it(state, tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: True
    )
    slot, fut = _pending(state, "r5", GRANTABLE)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json=_decide(slot, "r5", pattern=REF)
        )
        body = await resp.json()
    assert resp.status == 409 and body["code"] == "asks_every_time"
    assert not fut.done()
    assert _stored(tmp_path) == []


# -- what the card offers ---------------------------------------------------


def _event(**kw):
    base = dict(
        mcp_identity_trusted=True,
        mcp_server_name="notes",
        tool_name="lookup",
        is_shell=False,
        mcp_identity_unreadable=False,
        connection_mcp_identity=None,
        child_low_fidelity=False,
        child_mcp_identity_trusted=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_card_offers_a_verified_identity(no_refusal):
    assert always_allow_tool.card_fields(_event()) == GRANTABLE


@pytest.mark.parametrize(
    "kw",
    [
        {"mcp_identity_trusted": False},
        {"is_shell": True},
        {"mcp_identity_unreadable": True},
        {"connection_mcp_identity": ("notes", "lookup")},
        {"child_low_fidelity": True},
        {"mcp_server_name": "kirocrew-core"},
        {"tool_name": "look*"},
    ],
)
def test_card_offers_nothing_without_a_grantable_identity(no_refusal, kw):
    assert always_allow_tool.card_fields(_event(**kw)) == {}


def test_card_names_the_confirm_tools_refusal(monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: False)
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: True
    )
    assert always_allow_tool.card_fields(_event()) == {
        always_allow_tool.FIELD_REFUSAL: always_allow_tool.REFUSAL_ASKS_EVERY_TIME
    }


def test_card_names_the_local_overlay_refusal(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.platform.governance._operator_requires_confirmation", lambda r: False
    )
    monkeypatch.setattr("kiro_crew.config.loader.overlay_pins", lambda *k: True)
    assert always_allow_tool.card_fields(_event()) == {
        always_allow_tool.FIELD_REFUSAL: always_allow_tool.REFUSAL_SET_ELSEWHERE
    }


# -- the tool list's routes ------------------------------------------------


@pytest.mark.asyncio
async def test_tool_list_adds_lists_and_removes_one_grant(state, tmp_path, no_refusal):
    async with TestClient(TestServer(_app(state))) as client:
        added = await client.post("/api/security/tool-grants", json={"ref": REF})
        assert added.status == 200
        listed = await (await client.get("/api/security/tool-grants?server=notes")).json()
        assert listed == {"grants": [{"ref": REF}], "server": "notes", "refusal": ""}
        removed = await client.post("/api/security/tool-grants/remove", json={"ref": REF})
        assert (await removed.json())["grants"] == []
    assert _stored(tmp_path) == []


@pytest.mark.parametrize("ref", ["@notes/*", "@notes", "notes/lookup", "@a/b/c"])
@pytest.mark.asyncio
async def test_tool_list_refuses_a_ref_that_is_not_one_tool(state, tmp_path, no_refusal, ref):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/security/tool-grants", json={"ref": ref})
    assert resp.status == 400
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_tool_list_refuses_a_kiro_crew_server(state, tmp_path, no_refusal):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/security/tool-grants", json={"ref": "@kirocrew-core/spawn_run"}
        )
        code = (await resp.json())["code"]
        listed = await (await client.get("/api/security/tool-grants?server=kirocrew-core")).json()
    assert resp.status == 409 and code == "crew_owned"
    assert listed["refusal"] == "crew_owned"
    assert _stored(tmp_path) == []


@pytest.mark.asyncio
async def test_tool_list_writes_are_owner_only(state, tmp_path, no_refusal):
    async with TestClient(TestServer(_app(state, _app_caller))) as client:
        resp = await client.post("/api/security/tool-grants", json={"ref": REF})
    assert resp.status == 403
    assert _stored(tmp_path) == []
