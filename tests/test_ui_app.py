"""``_ensure_plugin_paths`` 的平台插件探测测试：pip、conda、缺失三类布局。

现场踩过两种坑：``pip install --target`` 没有 qt.conf，以及 conda 版 PyQt5 把插件
放在环境根的 ``Library/plugins``——兜底探测漏掉任何一种，用户看到的都是那句没有
信息量的原生弹窗。这里不建 QApplication，把 ``QCoreApplication`` 换成假对象记录
调用，验证探测顺序、环境变量兜底与"全落空给人话报错"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PyQt5 import QtCore

from hmi.ui import app as app_module


class _FakeCore:
    def __init__(self) -> None:
        self.existing: list[str] = []
        self.added: list[str] = []

    def libraryPaths(self) -> list[str]:
        return list(self.existing)

    def addLibraryPath(self, path: str) -> None:
        self.added.append(str(path))


def _with_platforms(root: Path) -> Path:
    plat = root / "platforms"
    plat.mkdir(parents=True, exist_ok=True)
    (plat / "qwindows.dll").write_bytes(b"MZ\x90\x00")
    return root


@pytest.fixture
def probe(monkeypatch, tmp_path):
    core = _FakeCore()
    monkeypatch.setattr(app_module, "QCoreApplication", core)
    monkeypatch.delenv("QT_QPA_PLATFORM_PLUGIN_PATH", raising=False)
    pkg = tmp_path / "site-packages" / "PyQt5"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(QtCore, "__file__", str(pkg / "QtCore.pyd"))
    prefix = tmp_path / "conda-env"
    monkeypatch.setattr(sys, "prefix", str(prefix))
    yield core, pkg, prefix
    os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)


def test_pip_wheel_layout_hit_first(probe) -> None:
    core, pkg, _prefix = probe
    hit = _with_platforms(pkg / "Qt5" / "plugins")
    app_module._ensure_plugin_paths()
    assert core.added == [str(hit)]
    assert os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] == str(hit / "platforms")


def test_legacy_plugins_dir_hit_second(probe) -> None:
    core, pkg, _prefix = probe
    hit = _with_platforms(pkg / "plugins")
    app_module._ensure_plugin_paths()
    assert core.added == [str(hit)]


def test_conda_library_layout_hit_third(probe) -> None:
    core, _pkg, prefix = probe
    hit = _with_platforms(prefix / "Library" / "plugins")
    app_module._ensure_plugin_paths()
    assert core.added == [str(hit)]
    assert os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] == str(hit / "platforms")


def test_existing_search_paths_short_circuit(probe) -> None:
    core, pkg, _prefix = probe
    ok = _with_platforms(pkg / "already-registered")
    core.existing = [str(ok)]
    app_module._ensure_plugin_paths()
    assert core.added == []
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in os.environ


def test_user_env_var_is_respected(probe, monkeypatch, tmp_path) -> None:
    core, _pkg, _prefix = probe
    mine = _with_platforms(tmp_path / "user-choice")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", str(mine))
    app_module._ensure_plugin_paths()
    assert core.added == []


def test_missing_everywhere_raises_readable_hint(probe) -> None:
    core, _pkg, _prefix = probe
    with pytest.raises(RuntimeError, match="start_gui.bat"):
        app_module._ensure_plugin_paths()
    assert core.added == []
