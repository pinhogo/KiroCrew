"""A parent blocked in ``spawn_sub_agents`` resumes when its last child ends,
whether or not the task store holds a row for that child.

Only a persistent run gets a store row. A run in a temporary or incognito chat
gets none, so the ledger cannot count its children: the wait is settled from the
parent's in-memory wait record and the manager's run records.

The manager, its admission pump and the durable store are real; the run is a
fake worker the test finishes on demand (``overload_fakes.ManagerHarness``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from overload_fakes import ManagerHarness

from kiro_crew.taskq import model
from kiro_crew.taskq.waits import WaitRecord

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_ROWLESS_MODES = ["temporary", "incognito"]


@pytest.fixture
def h():
    harness: ManagerHarness | None = None

    async def make(mode: str, max_concurrent: int = 2) -> ManagerHarness:
        nonlocal harness
        harness = ManagerHarness(max_concurrent)
        await harness.mgr.wait_taskq_ready()
        harness.mgr._memory_mode_for_session = lambda _key: mode
        return harness

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield make
        if harness is not None:
            harness.close()


async def _blocked_parent(hz: ManagerHarness):
    parent = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(parent)
    return parent


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _ROWLESS_MODES)
async def test_the_only_child_ending_resumes_the_parent(h, mode) -> None:
    hz = await h(mode)
    parent = await _blocked_parent(hz)
    child = hz.child_of(parent, "A")
    await hz.settle()
    assert hz.state(parent) is None and hz.state(child) is None, "neither run has a store row"
    assert hz.live(parent)._slot_released is True, "the parent yielded its slot to wait"

    await hz.end(child)
    await hz.settle()

    assert hz.live(parent)._slot_released is False
    assert hz.mgr._admission.resume_granted(parent.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _ROWLESS_MODES)
async def test_the_parent_waits_for_its_last_child(h, mode) -> None:
    hz = await h(mode, max_concurrent=3)
    parent = await _blocked_parent(hz)
    first = hz.child_of(parent, "A")
    second = hz.child_of(parent, "B")
    await hz.settle()

    await hz.end(first)
    await hz.settle()
    assert hz.live(parent)._slot_released is True, "B still runs"

    await hz.end(second)
    await hz.settle()
    assert hz.live(parent)._slot_released is False


@pytest.mark.asyncio
async def test_a_child_still_waiting_for_a_slot_keeps_the_parent_waiting(h) -> None:
    hz = await h("temporary", max_concurrent=1)
    parent = await _blocked_parent(hz)
    first = hz.child_of(parent, "A")
    second = hz.child_of(parent, "B")
    await hz.settle()
    assert second.id not in hz.mgr._agents, "B is queued behind A"

    await hz.end(first)
    await hz.settle()
    assert hz.live(parent)._slot_released is True, "B has not ended"

    await hz.end(hz.live(second))
    await hz.settle()
    assert hz.live(parent)._slot_released is False


@pytest.mark.asyncio
async def test_a_persistent_parent_still_resumes_through_the_ledger(h) -> None:
    hz = await h("persistent")
    parent = await _blocked_parent(hz)
    child = hz.child_of(parent, "A")
    await hz.settle()
    assert hz.state(parent) == model.WAITING_CHILDREN

    await hz.end(child)
    await hz.settle()

    assert hz.state(parent) == model.RUNNING
    assert hz.live(parent)._slot_released is False


class TestTheInMemoryCount:
    """``_rowless_wait_is_over`` on records built by hand."""

    @staticmethod
    def _admission(runs: dict):
        from kiro_crew.subagent_manager.admission.waits import _WaitsMixin

        return SimpleNamespace(
            _manager=SimpleNamespace(_agents=runs),
            _rowless_wait_is_over=_WaitsMixin._rowless_wait_is_over,
        )

    @staticmethod
    def _parent(awaited: list[str], **overrides):
        record = WaitRecord.children(awaited, since=0.0, tool_call_id="call", deadline_at=None)
        fields = dict(
            done=False,
            _slot_released=True,
            _wait_record=record.to_dict(),
            _deferred_released=set(),
        )
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def _over(self, parent, runs: dict, child_id: str = "a") -> bool:
        admission = self._admission(runs)
        return admission._rowless_wait_is_over(admission, parent, SimpleNamespace(id=child_id))

    def test_an_awaited_run_this_manager_does_not_hold_is_not_ended(self):
        assert self._over(self._parent(["a", "gone"]), {}) is False

    def test_an_awaited_run_that_is_not_done_is_not_ended(self):
        runs = {"b": SimpleNamespace(done=False)}
        assert self._over(self._parent(["a", "b"]), runs) is False

    def test_every_other_awaited_run_done_ends_the_wait(self):
        runs = {"b": SimpleNamespace(done=True)}
        assert self._over(self._parent(["a", "b"]), runs) is True

    def test_a_child_released_as_deferred_is_not_awaited(self):
        parent = self._parent(["a", "held"], _deferred_released={"held"})
        assert self._over(parent, {}) is True

    def test_a_parent_that_holds_its_slot_is_not_resumed(self):
        assert self._over(self._parent(["a"], _slot_released=False), {}) is False

    def test_a_finished_parent_is_not_resumed(self):
        assert self._over(self._parent(["a"], done=True), {}) is False

    def test_a_parent_with_no_wait_record_is_not_resumed(self):
        assert self._over(self._parent(["a"], _wait_record=None), {}) is False
