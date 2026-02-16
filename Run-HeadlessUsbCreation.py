import datetime
import importlib.util
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_SCRIPT = REPO_ROOT / "Create-MateBookWin11Usb.py"
RUN_LOG = REPO_ROOT / "HeadlessUsbRun.log"

ISO_PATH = r"C:\Users\georg\Downloads\Win11_25H2_EnglishInternational_x64.iso"
DRIVERS_PATH = r"D:\Matebook Drivers"
DRIVE_LETTER = "E"


def _load_app_module():
    spec = importlib.util.spec_from_file_location("matebook_usb_creator", APP_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec from: {APP_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def emit(message: str) -> None:
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    RUN_LOG.write_text("", encoding="utf-8")
    emit("Headless USB creation run started.")
    emit(f"ISO: {ISO_PATH}")
    emit(f"Drivers: {DRIVERS_PATH}")
    emit(f"Target USB: {DRIVE_LETTER}:")

    app = _load_app_module()
    app.configure_logging()
    if not app.is_user_admin():
        emit("Not running as Administrator. Requesting UAC elevation...")
        app.relaunch_as_admin()
        emit("Elevation requested; exiting unelevated parent process.")
        return 0

    def progress_callback(percent: int, message: str) -> None:
        emit(f"{int(percent):3d}% {message}")

    try:
        app.invoke_usb_creation(
            iso_path=ISO_PATH,
            drivers_path=DRIVERS_PATH,
            drive_letter=DRIVE_LETTER,
            prepared_driver_resolution=None,
            on_progress=progress_callback,
        )
        emit("RUN RESULT: SUCCESS")
        return 0
    except Exception as exc:
        emit(f"RUN RESULT: ERROR: {exc}")
        for line in traceback.format_exc().splitlines():
            emit(line)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
