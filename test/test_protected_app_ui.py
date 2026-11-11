"""The dashboard runs an app's UI scripts from the gateway's copy, not the app's tree.

``/apps/{name}/ui/{entry}`` is imported into the dashboard's own origin, and the app's
tree is writable from the sandbox. So a script is served from the copy the gateway
took when it installed the app (``apps.protected_app_ui``), under ``member-records/``,
which no sandboxed process can write.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source
from apps_routes_coverage_helpers import _make_app as _make_routes_app

from kiro_crew.apps import manager
from kiro_crew.apps import routes as app_routes
from kiro_crew.apps.manager import app_dir, apps_dir, install_app, uninstall_app, update_app

INSTALLED = b"export default () => 'installed'\n"
CHANGED = b"export default () => 'changed on disk'\n"


def _source_with_ui(tmp_path, *, name="test-app", version="1.0.0", body=INSTALLED):
    src = _make_app_source(
        tmp_path / version, name=name, version=version, ui={"entry": "index.mjs"}
    )
    (src / "ui" / "chunks").mkdir(parents=True)
    (src / "ui" / "index.mjs").write_bytes(body)
    (src / "ui" / "chunks" / "lazy.js").write_bytes(body)
    (src / "ui" / "style.css").write_bytes(b"body{}")
    return src


def _plant_folder(name: str, body: bytes = CHANGED) -> None:
    """An app folder no install put there: what any writer of ``apps/`` can make."""
    folder = apps_dir() / name
    (folder / "ui").mkdir(parents=True)
    (folder / "app.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (folder / "installed.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (folder / "ui" / "index.mjs").write_bytes(body)


async def _get(path: str) -> tuple[int, bytes]:
    app = web.Application()
    app.router.add_get("/apps/{name}/ui/{path:.*}", app_routes.handle_app_ui_file)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        return resp.status, await resp.read()


@pytest.mark.asyncio
async def test_a_script_changed_in_the_app_tree_is_served_as_installed(tmp_path, app_home):
    assert install_app(_source_with_ui(tmp_path)).ok
    ui = app_dir("test-app") / "ui"
    (ui / "index.mjs").write_bytes(CHANGED)
    (ui / "chunks" / "lazy.js").write_bytes(CHANGED)

    assert await _get("/apps/test-app/ui/index.mjs") == (200, INSTALLED)
    assert await _get("/apps/test-app/ui/chunks/lazy.js") == (200, INSTALLED)


@pytest.mark.asyncio
async def test_an_app_folder_planted_after_the_start_up_pass_has_no_scripts_served(app_home):
    """No request has to come first: the gateway's own pass at start closes the window."""
    from kiro_crew.apps import protected_app_ui

    protected_app_ui.capture_existing(apps_dir())  # what the gateway runs after its bind
    _plant_folder("forged")

    status, body = await _get("/apps/forged/ui/index.mjs")
    assert status == 404
    assert CHANGED not in body


@pytest.mark.asyncio
async def test_files_that_are_not_scripts_are_still_read_from_the_app_tree(tmp_path, app_home):
    assert install_app(_source_with_ui(tmp_path)).ok
    (app_dir("test-app") / "ui" / "style.css").write_bytes(b"body{color:red}")
    assert await _get("/apps/test-app/ui/style.css") == (200, b"body{color:red}")


@pytest.mark.asyncio
async def test_an_update_replaces_the_copy_and_an_uninstall_removes_it(tmp_path, app_home):
    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok
    newer = b"export default () => 'v2'\n"
    assert update_app(_source_with_ui(tmp_path, version="1.1.0", body=newer)).ok
    assert await _get("/apps/test-app/ui/index.mjs") == (200, newer)

    assert uninstall_app("test-app").ok
    assert not protected_app_ui.copy_root("test-app").exists()


@pytest.mark.asyncio
async def test_a_failed_update_keeps_the_copy_of_the_version_left_installed(
    tmp_path, app_home, monkeypatch
):
    assert install_app(_source_with_ui(tmp_path)).ok

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_write_installed", _fail)
    newer = b"export default () => 'v2'\n"
    result = update_app(_source_with_ui(tmp_path, version="1.1.0", body=newer))
    assert not result.ok
    assert await _get("/apps/test-app/ui/index.mjs") == (200, INSTALLED)


@pytest.mark.asyncio
async def test_an_app_an_older_build_installed_gets_its_copy_once(app_home):
    """Upgrading must not break installed apps: the start-up pass copies every one."""
    from kiro_crew.apps import protected_app_ui

    _plant_folder("older-app", INSTALLED)
    protected_app_ui.capture_existing(apps_dir())

    (apps_dir() / "older-app" / "ui" / "index.mjs").write_bytes(CHANGED)
    assert await _get("/apps/older-app/ui/index.mjs") == (200, INSTALLED)


def test_a_failed_start_up_copy_is_retried(app_home, monkeypatch):
    from kiro_crew.apps import protected_app_ui

    _plant_folder("older-app", INSTALLED)
    real_record = protected_app_ui.record

    def _full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(protected_app_ui, "record", _full_disk)
    protected_app_ui.capture_existing(apps_dir())
    assert protected_app_ui.served_root(apps_dir(), "older-app") is None

    _plant_folder("planted-later")
    monkeypatch.setattr(protected_app_ui, "record", real_record)
    assert protected_app_ui.served_root(apps_dir(), "older-app") is not None
    # The retry copies only the folders the first pass listed.
    assert protected_app_ui.served_root(apps_dir(), "planted-later") is None


def test_an_unreadable_script_fails_the_copy_instead_of_dropping_it(app_home, monkeypatch):
    from kiro_crew.apps import protected_app_ui

    _plant_folder("older-app", INSTALLED)
    real_open = protected_app_ui.os.open

    def _locked(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).endswith("index.mjs"):
            raise PermissionError("held open by another process")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(protected_app_ui.os, "open", _locked)
    protected_app_ui.capture_existing(apps_dir())
    assert not protected_app_ui.copy_root("older-app").exists()

    monkeypatch.setattr(protected_app_ui.os, "open", real_open)
    assert protected_app_ui.served_root(apps_dir(), "older-app") is not None


@pytest.mark.asyncio
async def test_an_owner_registration_takes_the_copy_of_a_self_managed_app(app_home):
    ui = apps_dir() / "self-app" / "ui"
    ui.mkdir(parents=True)
    (ui / "index.mjs").write_bytes(INSTALLED)
    async with TestClient(TestServer(_make_routes_app(dashboard_user="owner"))) as client:
        resp = await client.post(
            "/api/apps/register",
            json={"name": "self-app", "version": "1.0.0", "displayName": "Self App"},
        )
        assert resp.status == 201
        (ui / "index.mjs").write_bytes(CHANGED)
        resp = await client.get("/apps/self-app/ui/index.mjs")
        assert resp.status == 200
        assert await resp.read() == INSTALLED
