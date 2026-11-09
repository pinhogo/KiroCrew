"""Where a ledger write lands once its slot may have moved on: decided on the slot's lane.

A slot's newest unit carries the slot's fold state (``session/carried``), and the fold
replaces whatever the older units gave it with that state. A ledger write that names an
older unit AFTER such a carry would read wrongly either way: its entry sits in a unit the
carry already replaced, so it is lost, or that unit is published as the unit that
recorded last, so an unpinned read folds it AFTER the carry and lays its whole history
over the state the carry already holds.

So the ledger writers (``session_ledger.record_update`` and the Issue Radar ledger) hand
the whole write to ONE writer job (``emit_surfaces.on_ledger_recorded`` and
``on_radar_recorded``): it asks :func:`carrier_after`, appends, and publishes the unit
order for the unit the entry landed in. The job runs in the named unit's bucket, and the
writer holds one slot's units to one lane (``writer_groups``), which is where the
successor's carry is committed too (``emit_session._write_carries``, which calls
:func:`note_carried` before it publishes the carrier's order). So the carry lands wholly
before the job or wholly after it. Before, the job finds it and files the entry under the
slot's newest unit. After, the carry reads the entry: the older unit's tail moved since
the carry's fold read it, so the carry folds again. Nothing compares a clock, and nothing
guesses which entry is whose: the job hands its caller a :class:`Receipt` naming the
unit and seq the entry landed at.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import Final

logger = logging.getLogger(__name__)

#: Most ``(data home, slot, fold)`` carriers remembered, the oldest forgotten first: the
#: bound ``writer_groups.UNIT_SLOTS_LIMIT`` puts on the units the writer keys by slot.
CARRIERS_LIMIT: Final[int] = 4096

#: ``(data home, slot, fold)`` -> the unit whose carry of that fold landed last in this
#: process. Written on the slot's lane by the carry, read there by the ledger job.
_CARRIERS: "OrderedDict[tuple[str, str, str], str]" = OrderedDict()
_guard = threading.Lock()


class Receipt:
    """Where one ledger write landed, settled once by the writer job that made it.

    ``unit`` is the unit the entry is in (the slot's newest one when the job redirected
    it), ``seq`` its position there, and ``landed`` whether the append returned. Until it
    is settled, ``landed`` is False: an entry still queued or dropped is not in the file.
    Settled on the writer thread, read by the caller once :meth:`wait` (or a writer
    flush) returns.
    """

    __slots__ = ("unit", "seq", "landed", "_done", "_lock")

    def __init__(self, unit: str) -> None:
        self.unit = unit
        self.seq = 0
        self.landed = False
        self._done = threading.Event()
        self._lock = threading.Lock()

    def settle(self, unit: str, seq: int, *, landed: bool) -> None:
        """Record the outcome. Only the first call counts."""
        with self._lock:
            if self._done.is_set():
                return
            self.unit = unit
            self.seq = seq
            self.landed = landed
            self._done.set()

    @property
    def settled(self) -> bool:
        """Whether the job has said what became of the entry."""
        return self._done.is_set()

    def wait(self, timeout: "float | None") -> bool:
        """Wait at most *timeout* seconds for the outcome; True once it is settled."""
        return self._done.wait(timeout)


def note_carried(slot: str, unit: str, folds: "Iterable[str]") -> None:
    """Remember that *unit*'s carry of each of *folds* for *slot* has landed.

    Called by the carry on the slot's lane, right after its group commit and before the
    carrier's order is published, so a ledger job on the same lane that runs after it
    finds it.
    """
    if not slot or not unit:
        return
    home = _home()
    with _guard:
        for fold in folds:
            key = (home, slot, fold)
            _CARRIERS[key] = unit
            _CARRIERS.move_to_end(key)
        while len(_CARRIERS) > CARRIERS_LIMIT:
            _CARRIERS.popitem(last=False)


def clear() -> None:
    """Forget every carrier. For ``emit.reset_caches``, which stands in for a restart."""
    with _guard:
        _CARRIERS.clear()


def carrier_after(slot: str, unit_id: str, fold: str) -> str:
    """The slot's newest unit when *slot* moved past *unit_id* for *fold*, else ``""``.

    Moved past means a unit AFTER *unit_id* in the slot's succession carried *fold*
    (:func:`note_carried`), or *unit_id* is already closed ``superseded``: its successor
    carried every fold it held, and retention may collect it. ``""`` when *unit_id* is the
    slot's newest unit, is not in its succession (another spelling's unit), no later unit
    carried yet (a carry still to land reads the unit's entries when it does), or anything
    cannot be read: the write then goes where it always went.

    Cheap for the ordinary writer, which is the newest unit: the succession is cached and
    no log is opened. Exact on the slot's lane, where the ledger job asks it; asked off
    the lane it is a first guess the job asks again. A carry an earlier process wrote is
    seen only through the close it wrote after it (``emit_session._close_superseded``).
    """
    if not slot or not unit_id:
        return ""
    try:
        from kiro_crew.crew_log import projection

        units = projection.slot_succession(slot)
        if unit_id not in units or units[-1] == unit_id:
            return ""
        with _guard:
            carrier = _CARRIERS.get((_home(), slot, fold), "")
        if carrier in units[units.index(unit_id) + 1 :] or _closed_superseded(unit_id):
            return units[-1]
    except Exception:
        # Rendered to text: a kept traceback keeps this frame, and a crew log handle bound in it.
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log %s: could not tell whether slot %s moved on",
            unit_id,
            slot,
        )
    return ""


def _closed_superseded(unit_id: str) -> bool:
    """Whether *unit_id*'s newest lifecycle entry is a ``superseded`` close. A tail read."""
    from kiro_crew.crew_log.entry_types import SUPERSEDED_CLOSE_REASON
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import _newest_close, unit_dir_for

    directory = unit_dir_for(KIND_SESSION, unit_id)
    if directory is None:
        return False
    closed = _newest_close(directory)
    return closed is not None and closed.data.get("reason") == SUPERSEDED_CLOSE_REASON


def _home() -> str:
    from kiro_crew.config.paths import data_home

    return str(data_home())
