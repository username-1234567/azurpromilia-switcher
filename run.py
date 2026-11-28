"""PyInstaller 入口。

azp/__main__.py 使用相对导入，不能直接作为脚本入口；这里从包内导入 main。
打包后控制台代码页与输出编码不一致时中文会乱码，先统一切到 UTF-8。
"""

import sys

if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:  # noqa: BLE001
        pass

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 双击 exe 时没有参数：直接进图形界面，而不是打印命令帮助。
if getattr(sys, "frozen", False) and len(sys.argv) == 1:
    sys.argv.append("gui")

from azp.cli import main

if __name__ == "__main__":
    raise SystemExit(main())