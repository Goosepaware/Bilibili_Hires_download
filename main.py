"""双击启动图形界面；带参数时等同命令行。"""
import os
import sys
import traceback


def _ensure_cli_console() -> None:
    """打包成窗口程序后，命令行模式需要一个控制台。"""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    # 控制台版本或输出被重定向时，stdout 已经可用，不要另开控制台
    if sys.stdout is not None:
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        if k32.GetConsoleWindow():
            return
        if k32.AttachConsole(-1) == 0:
            k32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except Exception:
        pass


def _run():
    from bili_audio.cli import main

    return main(sys.argv[1:] or ["gui"])


if __name__ == "__main__":
    args = sys.argv[1:]
    gui_mode = not args or args == ["gui"]
    if not gui_mode:
        _ensure_cli_console()
    try:
        sys.exit(_run())
    except Exception:
        from pathlib import Path

        log = Path(os.environ.get("APPDATA", ".")) / "bili-audio" / "gui_error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, f"启动失败，详情见：\n{log}", "bili-audio", 0x10)
        except Exception:
            pass
        raise
