"""``kirocrew config set --file`` honours ``--local`` and warns on a stray key/value.

``--file`` replaces a whole document. With ``--local`` it writes the overlay
(``config.local.json``, whose values win over ``config.json``) instead of the base,
so the operator gets the overlay they asked for rather than an overwritten base.
A positional key/value has nothing well-defined to apply to beside a
whole-document ``--file`` write, so it is reported as ignored while the ``--file``
write still lands (the behaviour on main).

Every test writes under a temp data home; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cli_config import _config_cmd


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "config_action": "set",
        "key": None,
        "value": None,
        "file": None,
        "local": False,
        "value_base64": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _run(home: Path, args: argparse.Namespace) -> None:
    with (
        patch("kiro_crew.cli_config.config_path", return_value=home / "config.json"),
        patch(
            "kiro_crew.cli_config.config_local_path",
            return_value=home / "config.local.json",
        ),
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "crew"
    d.mkdir()
    return d


def test_file_with_local_writes_the_overlay_not_the_base(home, capsys):
    overlay_src = home / "overlay.json"
    overlay_src.write_text(json.dumps({"agent": {"provider": "acp"}}), encoding="utf-8")
    _run(home, _args(file=str(overlay_src), local=True))
    out = capsys.readouterr().out
    assert "✅" in out
    assert "config.local.json" in out
    # The overlay holds the document; the base is left untouched.
    written = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
    assert written["agent"] == {"provider": "acp"}
    assert not (home / "config.json").exists()


def test_file_with_local_overlay_is_not_meta_stamped(home):
    # An overlay is a partial document: it carries only the keys it pins, with no
    # meta stamp (the same as the keyed `config set --local` path).
    overlay_src = home / "overlay.json"
    overlay_src.write_text(json.dumps({"agent": {"provider": "acp"}}), encoding="utf-8")
    _run(home, _args(file=str(overlay_src), local=True))
    written = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
    assert "meta" not in written


def test_file_with_a_key_warns_and_still_writes_the_base(home, capsys):
    src = home / "whole.json"
    src.write_text(json.dumps({"agent": {"provider": "acp"}}), encoding="utf-8")
    _run(home, _args(key="agent.provider", value="acp", file=str(src)))
    captured = capsys.readouterr()
    # The key/value is reported ignored, not silently dropped.
    assert "ignoring the key/value" in captured.err
    assert "agent.provider" in captured.err
    # The --file write still landed on the base (back-compat).
    assert "✅" in captured.out
    written = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert written["agent"] == {"provider": "acp"}


def test_plain_file_still_replaces_the_base_config(home, capsys):
    # Back-compat: ``--file`` alone is unchanged.
    src = home / "whole.json"
    src.write_text(json.dumps({"agent": {"provider": "acp"}}), encoding="utf-8")
    _run(home, _args(file=str(src)))
    out = capsys.readouterr().out
    assert "✅" in out
    written = json.loads((home / "config.json").read_text(encoding="utf-8"))
    # The file's document is installed whole (the loader adds a meta stamp).
    assert written["agent"] == {"provider": "acp"}
    assert not (home / "config.local.json").exists()
