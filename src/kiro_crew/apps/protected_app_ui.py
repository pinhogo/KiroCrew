"""The copy of each app's UI scripts that the dashboard runs, kept where the app cannot write.

The dashboard imports an app's UI module (``/apps/<name>/ui/<entry>``) into its own
origin, so a script served from there runs with the owner's session. The installed
tree, ``apps/<name>/``, is writable from the sandbox, and the directory can be renamed
aside and recreated, so the bytes on disk there are not the bytes the gateway installed.

So the gateway keeps its own copy of every script (``.js`` and ``.mjs``) under
``<data home>/member-records/app-ui/<name>/``, which the OS sandbox masks in every mode
and the agent file tools refuse (:mod:`kiro_crew.member_records`). The gateway takes the
copy inside its own transactions: an install and an update (an adoption runs one) copy
the scripts from the source they install, and an owner's registration of a
self-managed app copies them from the app's folder. The UI route serves scripts from
that copy; every other UI file (styles, images, fonts, data) is still read from the
app's own tree, as before.

A gateway that ran an older build has no copies yet. Right after the gateway's
listener binds, every installed app gets its copy taken from its tree as it stands,
once (:func:`capture_existing`). After that an app the gateway never installed has no
copy, and its scripts are not served.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

from kiro_crew import member_records
from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.platform_lock_compat import acquire_lock, open_create_or_existing, release_lock

logger = logging.getLogger(__name__)

#: Record kind under ``member-records/``; it is in ``member_records.RECORD_KINDS``.
APP_UI_KIND = "app-ui"

#: UI files the dashboard runs as code. Only these are served from the copy.
SCRIPT_EXTENSIONS: frozenset[str] = frozenset({".js", ".mjs"})

#: Written once every app installed by an older build has its copy.
CAPTURED_MARKER = ".captured-from-app-trees"
_CAPTURE_LOCK = ".capturing"
#: The app folders the first pass found; every retry copies only these.
_CAPTURE_LIST = ".capture-list.json"
_TMP_PREFIX = ".tmp-"
_OLD_PREFIX = ".old-"

#: The UI route refuses larger files (``routes_static._UI_MAX_BYTES``), so none is kept.
_MAX_BYTES = 8 * 1024 * 1024
_INSTALLED_META = "installed.json"


def _kind_dir() -> Path:
    return member_records.records_dir(APP_UI_KIND)


def copy_root(app_name: str) -> Path:
    """``member-records/app-ui/<app>``, containment-checked. Raises ``ValueError``."""
    if not isinstance(app_name, str) or not APP_NAME_RE.fullmatch(app_name):
        raise ValueError(f"unsafe app name: {app_name!r}")
    root = _kind_dir().resolve()
    target = (root / app_name).resolve()
    if target.parent != root:
        raise ValueError(f"app name {app_name!r} escapes {root}")
    return target


def _copy_scripts(src_root: Path, dest_root: Path) -> None:
    """Copy every regular script file under *src_root* into *dest_root*.

    Links are never followed, at a file or a directory, so nothing outside the tree
    is read. shortcut: a script reached only through an in-tree directory link is not
    copied, so it is not served; follow contained links if an app ever needs one.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)

    def _raise(exc: OSError) -> None:
        raise exc

    for dir_path, dirs, files in os.walk(src_root, onerror=_raise, followlinks=False):
        # A Windows junction is not a symlink to os.walk, so it is pruned by name.
        dirs[:] = [d for d in dirs if not is_link_or_junction(Path(dir_path) / d)]
        rel_dir = Path(dir_path).relative_to(src_root)
        for file_name in files:
            if Path(file_name).suffix.lower() not in SCRIPT_EXTENSIONS:
                continue
            if is_link_or_junction(Path(dir_path) / file_name):
                continue
            try:
                fd = os.open(os.path.join(dir_path, file_name), flags)
            except ValueError:
                # A name the OS cannot encode: the route could never open it either.
                continue
            # Any other open failure raises: a copy missing a readable script would
            # answer 404 for it for good, so the caller keeps its previous copy.
            # The descriptor's own path must still sit inside the tree: a parent
            # swapped for a link after the walk listed it is refused, not followed.
            real = fd_real_path(fd)
            if real is None or not Path(real).is_relative_to(src_root):
                os.close(fd)
                continue
            with os.fdopen(fd, "rb") as src:
                st = os.fstat(src.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _MAX_BYTES:
                    continue
                data = bounded_read(src, _MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                continue
            out_dir = dest_root / rel_dir
            mkdirs_owner_only(out_dir)
            (out_dir / file_name).write_bytes(data)


def record(app_name: str, ui_dir: Path, container: Path) -> Path | None:
    """Replace *app_name*'s copy with the scripts in *ui_dir*; return the previous copy.

    *container* is the tree *ui_dir* belongs to: a *ui_dir* that resolves outside it
    (a dev-mode link, an escaping link) copies nothing. The previous copy is kept
    aside and returned, so a caller inside a transaction can put it back
    (:func:`restore`) or drop it (:func:`discard`). Raises ``OSError`` or
    ``ValueError`` with nothing changed.
    """
    target = copy_root(app_name)
    ui_real = Path(os.path.realpath(ui_dir))
    kind = target.parent
    mkdirs_owner_only(kind)
    tmp = kind / f"{_TMP_PREFIX}{app_name}-{secrets.token_hex(4)}"
    mkdirs_owner_only(tmp)
    previous: Path | None = None
    try:
        if ui_real.is_relative_to(os.path.realpath(container)) and ui_real.is_dir():
            _copy_scripts(ui_real, tmp)
        if target.exists():
            previous = kind / f"{_OLD_PREFIX}{app_name}-{secrets.token_hex(4)}"
            os.replace(target, previous)
        try:
            os.replace(tmp, target)
        except OSError:
            if previous is not None:
                os.replace(previous, target)
            raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return previous


def restore(app_name: str, previous: Path | None) -> None:
    """Put back the copy :func:`record` set aside, for a rolled-back update."""
    target = copy_root(app_name)
    shutil.rmtree(target, ignore_errors=True)
    if previous is not None:
        os.replace(previous, target)


def discard(previous: Path | None) -> None:
    """Drop the copy :func:`record` set aside, once the change it replaced commits."""
    if previous is not None:
        shutil.rmtree(previous, ignore_errors=True)


def forget(app_name: str) -> None:
    """Remove *app_name*'s copy, for an uninstall."""
    with contextlib.suppress(OSError, ValueError):
        shutil.rmtree(copy_root(app_name))


def record_folder(apps_root: Path, app_name: str) -> None:
    """Replace *app_name*'s copy with the scripts in its own folder under *apps_root*.

    For a folder the gateway did not fill itself: an owner's registration of a
    self-managed app, and the start-up capture below. Raises ``OSError`` or
    ``ValueError`` with nothing changed.
    """
    # Anchored on the apps root, never resolved through the app's own entry, so a
    # link planted at ``apps/<name>`` makes its ``ui`` land outside and copy nothing.
    folder = Path(os.path.realpath(apps_root)) / app_name
    discard(record(app_name, folder / "ui", folder))


@contextlib.contextmanager
def _capture_lock() -> Iterator[None]:
    path = _kind_dir() / _CAPTURE_LOCK
    mkdirs_owner_only(path.parent)
    fd = open_create_or_existing(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        acquire_lock(fd, exclusive=True)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _eligible_names(apps_root: Path) -> list[str]:
    """The app folders with a record, at the moment of the first pass."""
    names: list[str] = []
    entries = sorted(apps_root.iterdir()) if apps_root.is_dir() else []
    for entry in entries:
        if not APP_NAME_RE.fullmatch(entry.name) or is_link_or_junction(entry):
            continue
        if entry.is_dir() and (entry / _INSTALLED_META).is_file():
            names.append(entry.name)
    return names


def capture_existing(apps_root: Path) -> None:
    """Give every app an older build installed its copy. Blocking.

    The gateway runs it once right after its listener binds, after the sweep of
    interrupted updates, and a script read that comes first runs it itself. The
    first pass writes the list of app folders it found, and every retry works from
    that list only, so a folder made later is never taken in. Each app is copied
    under its record lock, the lock an install or update holds, and one that already
    has a copy is left as it is. The marker is written only when every listed app got
    its copy, so a failure is retried at the next call. shortcut: an app tree
    changed before the first pass is copied as it stands, since no earlier copy
    exists to compare it with.
    """
    # The facade, not the owner: only the manager family imports its owners.
    from kiro_crew.apps.manager import app_record_lock

    kind = _kind_dir()
    marker = kind / CAPTURED_MARKER
    if marker.is_file():
        return
    with _capture_lock():
        if marker.is_file():
            return
        listed = kind / _CAPTURE_LIST
        if listed.is_file():
            names = json.loads(listed.read_text(encoding="utf-8"))
        else:
            names = _eligible_names(apps_root)
            atomic_write(listed, json.dumps(names) + "\n")
        failed = False
        for name in names:
            try:
                with app_record_lock(name):
                    if not copy_root(name).exists():
                        record_folder(apps_root, name)
            except (OSError, ValueError):
                failed = True
                logger.warning(
                    "could not keep a copy of app %s's UI scripts; its scripts are not "
                    "served until the next start retries",
                    name,
                    exc_info=True,
                )
        if not failed:
            atomic_write(marker, "1\n")


def served_root(apps_root: Path, app_name: str) -> Path | None:
    """The directory *app_name*'s scripts are served from, or ``None`` when it has none.

    Takes the one-time copy of older installs itself when the gateway's start-up
    pass has not finished yet (:func:`capture_existing`).
    """
    try:
        capture_existing(apps_root)
    except OSError:
        logger.warning("could not finish copying installed apps' UI scripts", exc_info=True)
    try:
        root = copy_root(app_name)
    except (OSError, ValueError):
        return None
    return root if root.is_dir() else None
