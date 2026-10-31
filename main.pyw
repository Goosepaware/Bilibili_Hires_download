import os
import sys
import traceback


def _run():
    from bili_audio.cli import main

    return main(sys.argv[1:] or ["gui"])


if __name__ == "__main__":
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
