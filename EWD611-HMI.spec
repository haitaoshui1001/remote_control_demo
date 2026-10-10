# mode: python
"""EWD611 上位机打包配方：onedir + windowed（无控制台）。

为什么 onedir 而不是 onefile：本项目拖着 PyQt5 + PyAV(ffmpeg) + numpy，
onefile 每次启动都要把几百 MB 解到临时目录，现场机器上杀毒软件还会拦；
onedir 首启快、崩溃日志路径稳定，代价只是交付时要整个目录一起拷。

config/ 不打进 _internal：现场要改 IP/串口波特率，构建脚本会把 config/
原样拷到 dist/EWD611-HMI/ 旁边，双击 exe 即可编辑。
"""

# ruff: noqa: F821  (SPECPATH/Analysis 等由 PyInstaller 注入执行环境)
import sys
from pathlib import Path

ROOT = Path(SPECPATH)


def _env_libexpat() -> str | None:
    """构建解释器自带的 libexpat.dll——和 pyexpat.pyd 是同一份 Conda 构建。

    ONVIF 客户端用标准库 xml.etree.ElementTree 解 SOAP 应答，而
    ``pyexpat.pyd`` 运行时要加载 ``libexpat.dll``。PyInstaller 按 DLL 搜索
    顺序收集依赖，实测会收进另一份版本（413KB vs Conda env 的 279KB），
    冻结包里就变成::

        ImportError: DLL load failed while importing pyexpat
        ImportError: No module named expat; use SimpleXMLTreeBuilder instead

    现象是"读摄像头参数一直读不到"：ElementTree 不可用，解析处抛的
    ImportError 不属于 OnvifError，工作线程静默死亡，界面无结果。
    所以这里强制用 ``sys.prefix`` 下那份，保证与 pyexpat.pyd 同源。
    """
    root = Path(sys.prefix)
    for rel in ("Library/bin", "DLLs", "libs", "."):
        cand = root / rel / "libexpat.dll"
        if cand.is_file():
            return str(cand)
    return None


def _pin_libexpat(binaries: list) -> list:
    """剔除自动收集的所有 libexpat.dll，只保留与解释器同源的那一份。"""
    good = _env_libexpat()
    kept = [b for b in binaries if Path(str(b[0])).name.lower() != "libexpat.dll"]
    if good is None:
        print("[spec] WARNING: 未在本机解释器目录找到 libexpat.dll，沿用自动收集结果")
        return kept
    kept.append(("libexpat.dll", good, "BINARY"))
    print(f"[spec] libexpat.dll pinned to {good}")
    return kept

a = Analysis(
    [str(ROOT / "entry_gui.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        # 反射/条件导入、PyInstaller 静态分析够不着的
        "av.audio.frame",
        "av.video.frame",
        "serial.tools.list_ports",
        "serial.tools.list_ports_common",
        "PIL.ImageQt",
        # 页面模块由 hmi.ui.pages.create_page 用 importlib.import_module(可变名)
        # 动态加载，静态分析完全看不到；漏了它们打包后只会得到占位页。
        # 新增/改名页面时，这份清单要和 hmi/ui/pages/__init__.py 的 PAGES 对齐。
        "hmi.ui.pages.video_page",
        "hmi.ui.pages.console_page",
        "hmi.ui.pages.data_page",
        "hmi.ui.pages.radio_page",
        "hmi.ui.pages.channel_page",
        "hmi.ui.pages.log_page",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 测试与开发工具不进交付包
        "pytest",
        "_pytest",
        "pygments",
        "ruff",
        "tkinter",
        "unittest",
        "pydoc_data",
    ],
    noarchive=False,
)

# 依赖收集完成后立刻纠正 libexpat.dll（见 _env_libexpat 的说明）。
a.binaries = _pin_libexpat(list(a.binaries))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EWD611-HMI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="EWD611-HMI",
)
