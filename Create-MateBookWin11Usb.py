from __future__ import annotations

import ctypes
import json
import locale
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from tkinter import StringVar, Tk, filedialog, messagebox
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText
    TK_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    StringVar = Tk = filedialog = messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    ScrolledText = None  # type: ignore[assignment]
    TK_IMPORT_ERROR = exc


DEFAULT_ISO_PATH = r"C:\Users\georg\Downloads\Win11_25H2_EnglishInternational_x64.iso"
DEFAULT_DRIVERS_PATH = r"D:\Matebook Drivers"
DEFAULT_USB_LETTER = "E"
SCRIPT_PATH = Path(__file__).resolve()
LOG_PATH = SCRIPT_PATH.with_suffix(".log")
PORTABLE_7ZIP_DIR = SCRIPT_PATH.parent / "tools" / "7zip-portable"

LOGGER = logging.getLogger("matebook_win11_usb_creator")


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: list[str]


@dataclass(frozen=True)
class DriverPackage:
    relative_path: str
    full_path: str
    file_name: str


@dataclass(frozen=True)
class ThirdPartyDriver:
    published_name: str
    original_file_name: str
    provider_name: str
    class_name: str
    date: str
    version: str


class CommandError(RuntimeError):
    pass


def configure_logging() -> None:
    if LOGGER.handlers:
        return

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    except Exception:
        return

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.info("----- Session start -----")


def log_info(message: str) -> None:
    try:
        LOGGER.info(message)
    except Exception:
        pass


def log_exception(message: str) -> None:
    try:
        LOGGER.exception(message)
    except Exception:
        pass


def show_startup_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "MateBook Win11 USB Creator", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def is_user_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    script_path = str(Path(sys.argv[0]).resolve())
    launch_arguments = [script_path, *sys.argv[1:]]
    params = subprocess.list2cmdline(launch_arguments)
    working_directory = str(Path(script_path).parent)
    log_info(
        "Requesting UAC elevation. "
        f"Executable={sys.executable} Script={script_path} Cwd={os.getcwd()}"
    )
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        params,
        working_directory,
        1,
    )
    if result <= 32:
        raise RuntimeError("Failed to request Administrator elevation.")


def get_oem_encoding() -> str:
    try:
        code_page = int(ctypes.windll.kernel32.GetOEMCP())
        return f"cp{code_page}"
    except Exception:
        return locale.getpreferredencoding(False) or "utf-8"


def derive_robocopy_exit_code(output_lines: list[str]) -> int | None:
    summary_found = False
    failed_detected = False
    pattern = re.compile(r"^\s*(Dirs|Files)\s*:\s*\S+\s+\S+\s+\S+\s+\S+\s+(\S+)", re.IGNORECASE)

    for line in output_lines:
        if not line:
            continue
        match = pattern.match(line)
        if not match:
            continue
        summary_found = True
        failed_token = re.sub(r"[^\d]", "", match.group(2))
        if failed_token and int(failed_token) > 0:
            failed_detected = True
            break

    if summary_found:
        return 8 if failed_detected else 1
    return None


def run_command(
    file_path: str,
    arguments: list[str] | None = None,
    *,
    error_message: str,
    accept_exit_codes: set[int] | None = None,
    on_output_line: callable | None = None,
) -> CommandResult:
    if arguments is None:
        arguments = []
    if accept_exit_codes is None:
        accept_exit_codes = {0}

    output: list[str] = []
    encoding = get_oem_encoding()
    command = [file_path, *arguments]
    command_text = " ".join(command)
    log_info(f"Running command: {command_text}")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=encoding,
        errors="replace",
        bufsize=1,
    )

    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\r\n")
        output.append(line)
        if on_output_line:
            on_output_line(line)

    process.wait()
    exit_code = process.returncode

    if exit_code is None and os.path.basename(file_path).lower() == "robocopy.exe":
        exit_code = derive_robocopy_exit_code(output)

    if exit_code is None:
        tail = "\n".join(output[-20:])
        log_info(f"Command returned no exit code: {command_text}")
        raise CommandError(
            f"{error_message}\nExit code: <unavailable>\nCommand: {file_path} {' '.join(arguments)}\n{tail}"
        )

    if int(exit_code) not in accept_exit_codes:
        tail = "\n".join(output[-20:])
        log_info(f"Command failed with exit code {exit_code}: {command_text}")
        raise CommandError(
            f"{error_message}\nExit code: {exit_code}\nCommand: {file_path} {' '.join(arguments)}\n{tail}"
        )

    log_info(f"Command completed with exit code {exit_code}: {command_text}")
    return CommandResult(exit_code=int(exit_code), output=output)


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_powershell(
    command: str,
    *,
    error_message: str,
    accept_exit_codes: set[int] | None = None,
    on_output_line: callable | None = None,
) -> CommandResult:
    if accept_exit_codes is None:
        accept_exit_codes = {0}
    return run_command(
        "powershell.exe",
        [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        error_message=error_message,
        accept_exit_codes=accept_exit_codes,
        on_output_line=on_output_line,
    )


def parse_json_output(output_lines: list[str]) -> object:
    payload = "\n".join(line for line in output_lines if line and line.strip())
    if not payload:
        return []
    return json.loads(payload)


def clear_readonly_attributes(*, paths: list[Path], on_progress: callable | None, percent: int) -> None:
    existing_files = [path for path in paths if path.exists()]
    if not existing_files:
        return

    write_stage(
        percent=percent,
        message="Clearing read-only attributes on setup image files...",
        on_progress=on_progress,
    )

    for path in existing_files:
        run_command(
            "attrib.exe",
            ["-R", str(path)],
            error_message=f"Failed to clear read-only attribute: {path}",
        )
        write_stage(percent=percent, message=f"Writable image file: {path}", on_progress=on_progress)


def write_stage(*, percent: int, message: str, on_progress: callable | None) -> None:
    bounded = max(0, min(100, int(percent)))
    if on_progress:
        on_progress(bounded, message)


def write_command_output(
    *,
    line: str | None,
    percent: int,
    prefix: str,
    on_progress: callable | None,
    robocopy_only: bool = False,
) -> None:
    if line is None:
        return
    clean_line = line.strip()
    if not clean_line:
        return

    if robocopy_only:
        should_show = False
        if re.search(r"^\d+(\.\d+)?%", clean_line):
            should_show = True
        if re.search(r"\b(New File|New Dir|Older|Newer|Changed|Extra|Same)\b", clean_line):
            should_show = True
        if re.search(r"^(Started|Ended|Source|Dest|Files|Options|Bytes|Times|Speed|Total|Dirs)\b", clean_line):
            should_show = True
        if not should_show:
            return

    write_stage(percent=percent, message=f"{prefix}{clean_line}", on_progress=on_progress)


def get_wim_indexes(wim_path: str) -> list[int]:
    if not Path(wim_path).exists():
        raise RuntimeError(f"WIM image not found: {wim_path}")

    result = run_command(
        "dism.exe",
        ["/English", "/Get-WimInfo", f"/WimFile:{wim_path}"],
        error_message=f"Unable to read image indexes from {wim_path}",
    )

    indexes: list[int] = []
    pattern = re.compile(r"^\s*Index\s*:\s*(\d+)\s*$", re.IGNORECASE)
    for line in result.output:
        match = pattern.match(line)
        if match:
            indexes.append(int(match.group(1)))

    if not indexes:
        raise RuntimeError(f"No image indexes found in: {wim_path}")
    return indexes


def get_driver_repository_inf_files(drivers_path: str) -> list[DriverPackage]:
    root = Path(drivers_path)
    if not root.exists():
        raise RuntimeError(f"Driver folder not found: {drivers_path}")

    root_resolved = root.resolve()
    inf_files = sorted(root_resolved.rglob("*.inf"), key=lambda p: str(p).lower())
    entries: list[DriverPackage] = []
    for file_path in inf_files:
        relative_path = str(file_path.relative_to(root_resolved))
        entries.append(
            DriverPackage(
                relative_path=relative_path,
                full_path=str(file_path),
                file_name=file_path.name,
            )
        )
    return entries


def summarize_driver_extensions(root: Path, max_items: int = 8) -> str:
    counts: dict[str, int] = {}
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        extension = file_path.suffix.lower() or "<noext>"
        counts[extension] = counts.get(extension, 0) + 1

    if not counts:
        return "no files found"

    top_items = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:max_items]
    return ", ".join(f"{ext}={count}" for ext, count in top_items)


def extract_driver_archives(
    *,
    drivers_root: Path,
    on_progress: callable | None,
    percent: int,
) -> Path:
    zip_files = sorted(
        [path for path in drivers_root.rglob("*") if path.is_file() and path.suffix.lower() == ".zip"],
        key=lambda path: str(path).lower(),
    )
    if not zip_files:
        raise RuntimeError("No .zip archives were found to extract.")

    extraction_root = Path(tempfile.mkdtemp(prefix="MateBookDriverZipExtract_"))
    write_stage(
        percent=percent,
        message=f"No .inf files found directly. Extracting {len(zip_files)} zip archive(s) to temporary folder...",
        on_progress=on_progress,
    )
    log_info(f"Extracting {len(zip_files)} driver zip archive(s) into: {extraction_root}")

    for index, zip_path in enumerate(zip_files, start=1):
        relative_zip = zip_path.relative_to(drivers_root)
        destination_folder = extraction_root / relative_zip.parent / zip_path.stem
        destination_folder.mkdir(parents=True, exist_ok=True)
        write_stage(
            percent=percent,
            message=f"Extracting archive [{index}/{len(zip_files)}]: {relative_zip}",
            on_progress=on_progress,
        )

        try:
            with zipfile.ZipFile(zip_path, mode="r") as archive:
                archive.extractall(destination_folder)
        except Exception as exc:
            raise RuntimeError(f"Failed to extract archive: {zip_path}\n{exc}") from exc

    return extraction_root


def find_installed_7zip_executable() -> str | None:
    candidates = ["7z.exe", "7z", "7za.exe", "7za"]
    for name in candidates:
        resolved = shutil.which(name)
        if resolved:
            return resolved

    common_locations = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "7-Zip" / "7z.exe",
    ]
    for candidate in common_locations:
        if candidate.exists():
            return str(candidate)
    return None


def ensure_portable_7zip() -> str | None:
    portable_candidates = [
        PORTABLE_7ZIP_DIR / "7z.exe",
        PORTABLE_7ZIP_DIR / "7za.exe",
        PORTABLE_7ZIP_DIR / "7zz.exe",
        PORTABLE_7ZIP_DIR / "7zr.exe",
    ]
    for candidate in portable_candidates:
        if candidate.exists():
            return str(candidate)

    installed = find_installed_7zip_executable()
    if not installed:
        return None

    installed_path = Path(installed)
    try:
        PORTABLE_7ZIP_DIR.mkdir(parents=True, exist_ok=True)
        target_exe = PORTABLE_7ZIP_DIR / installed_path.name
        shutil.copy2(installed_path, target_exe)

        # 7z.exe requires 7z.dll next to it.
        if installed_path.name.lower() == "7z.exe":
            installed_dll = installed_path.parent / "7z.dll"
            if installed_dll.exists():
                shutil.copy2(installed_dll, PORTABLE_7ZIP_DIR / "7z.dll")

        log_info(f"Portable 7-Zip prepared at: {target_exe}")
        return str(target_exe)
    except Exception:
        log_exception("Failed to prepare portable 7-Zip bundle.")
        return installed


def find_7zip_executable() -> str | None:
    portable = ensure_portable_7zip()
    if portable:
        return portable
    return find_installed_7zip_executable()


def extract_driver_installers_with_7zip(
    *,
    source_root: Path,
    destination_root: Path,
    seven_zip_path: str,
    on_progress: callable | None,
    percent: int,
) -> Path:
    exe_files = sorted(
        [path for path in source_root.rglob("*") if path.is_file() and path.suffix.lower() == ".exe"],
        key=lambda path: str(path).lower(),
    )
    if not exe_files:
        raise RuntimeError("No .exe installers were found to extract with 7-Zip.")

    extraction_root = destination_root / "exe_extracted"
    extraction_root.mkdir(parents=True, exist_ok=True)
    write_stage(
        percent=percent,
        message=f"Found {len(exe_files)} installer EXE file(s). Attempting 7-Zip extraction...",
        on_progress=on_progress,
    )
    log_info(f"Using 7-Zip at {seven_zip_path} to extract {len(exe_files)} EXE installer(s).")

    for index, exe_path in enumerate(exe_files, start=1):
        relative_exe = exe_path.relative_to(source_root)
        destination_folder = extraction_root / relative_exe.parent / exe_path.stem
        destination_folder.mkdir(parents=True, exist_ok=True)

        write_stage(
            percent=percent,
            message=f"7-Zip extracting installer [{index}/{len(exe_files)}]: {relative_exe}",
            on_progress=on_progress,
        )

        run_command(
            seven_zip_path,
            ["x", "-y", f"-o{destination_folder}", str(exe_path)],
            error_message=f"Failed to extract installer with 7-Zip: {exe_path}",
            accept_exit_codes={0},
        )

    return extraction_root


def read_inf_text(full_path: str) -> str:
    data = Path(full_path).read_bytes()
    return data.decode("latin-1", errors="ignore")


def get_wifi_driver_packages(driver_packages: list[DriverPackage]) -> list[DriverPackage]:
    wifi_packages: list[DriverPackage] = []
    seen: set[tuple[str, str]] = set()

    for package in driver_packages:
        try:
            inf_content = read_inf_text(package.full_path)
        except Exception:
            continue

        is_network_class = bool(
            re.search(r"(?im)^\s*Class\s*=\s*Net\s*$", inf_content)
            or re.search(r"(?im)^\s*ClassGuid\s*=\s*\{4d36e972-e325-11ce-bfc1-08002be10318\}\s*$", inf_content)
        )
        if not is_network_class:
            continue

        has_wireless_marker = bool(
            re.search(r"(?i)\b(wi-?fi|wlan|wireless|802\.11)\b", inf_content)
            or re.search(r"(?i)(wifi|wlan|wireless|80211|netwtw|rtwl|athw|mtkwl|qcwlan)", package.file_name)
        )
        if not has_wireless_marker:
            continue

        identity = (package.file_name.lower(), package.relative_path.lower())
        if identity in seen:
            continue
        seen.add(identity)
        wifi_packages.append(package)

    wifi_packages.sort(key=lambda item: (item.file_name.lower(), item.relative_path.lower()))
    return wifi_packages


def get_image_third_party_drivers(image_path: str) -> list[ThirdPartyDriver]:
    result = run_command(
        "dism.exe",
        ["/English", f"/Image:{image_path}", "/Get-Drivers"],
        error_message=f"Unable to list third-party drivers in mounted image: {image_path}",
    )

    drivers: list[ThirdPartyDriver] = []
    current: dict[str, str] | None = None

    for raw_line in result.output:
        line = raw_line.strip()
        if not line:
            continue

        published_match = re.match(r"(?i)^Published Name\s*:\s*(.+)$", line)
        if published_match:
            if current:
                drivers.append(
                    ThirdPartyDriver(
                        published_name=current.get("published_name", ""),
                        original_file_name=current.get("original_file_name", ""),
                        provider_name=current.get("provider_name", ""),
                        class_name=current.get("class_name", ""),
                        date=current.get("date", ""),
                        version=current.get("version", ""),
                    )
                )
            current = {
                "published_name": published_match.group(1).strip(),
                "original_file_name": "",
                "provider_name": "",
                "class_name": "",
                "date": "",
                "version": "",
            }
            continue

        if current is None:
            continue

        original_match = re.match(r"(?i)^Original File Name\s*:\s*(.+)$", line)
        if original_match:
            current["original_file_name"] = original_match.group(1).strip()
            continue

        provider_match = re.match(r"(?i)^Provider Name\s*:\s*(.+)$", line)
        if provider_match:
            current["provider_name"] = provider_match.group(1).strip()
            continue

        class_match = re.match(r"(?i)^Class Name\s*:\s*(.+)$", line)
        if class_match:
            current["class_name"] = class_match.group(1).strip()
            continue

        date_match = re.match(r"(?i)^Date\s*:\s*(.+)$", line)
        if date_match:
            current["date"] = date_match.group(1).strip()
            continue

        version_match = re.match(r"(?i)^Version\s*:\s*(.+)$", line)
        if version_match:
            current["version"] = version_match.group(1).strip()
            continue

    if current:
        drivers.append(
            ThirdPartyDriver(
                published_name=current.get("published_name", ""),
                original_file_name=current.get("original_file_name", ""),
                provider_name=current.get("provider_name", ""),
                class_name=current.get("class_name", ""),
                date=current.get("date", ""),
                version=current.get("version", ""),
            )
        )

    return drivers


def clear_directory(path: str) -> None:
    folder = Path(path)
    if not folder.exists():
        return
    for child in folder.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except FileNotFoundError:
                pass


def add_drivers_to_wim(
    *,
    wim_path: str,
    indexes: list[int],
    drivers_path: str,
    mount_dir: str,
    start_percent: int,
    end_percent: int,
    label: str,
    required_original_inf_names: list[str] | None,
    required_original_inf_label: str,
    on_progress: callable | None,
) -> None:
    driver_packages = get_driver_repository_inf_files(drivers_path)
    if not driver_packages:
        raise RuntimeError(f"No .inf driver packages found under: {drivers_path}")

    write_stage(
        percent=start_percent,
        message=f"{label}: discovered {len(driver_packages)} INF packages in driver repository.",
        on_progress=on_progress,
    )
    for index, package in enumerate(driver_packages, start=1):
        write_stage(
            percent=start_percent,
            message=f"{label}: package [{index}/{len(driver_packages)}] {package.relative_path}",
            on_progress=on_progress,
        )

    required_inf_names = sorted({name.strip() for name in (required_original_inf_names or []) if name.strip()})
    required_lookup = {name.lower(): name for name in required_inf_names}

    total_indexes = max(1, len(indexes))
    percent_range = max(1, end_percent - start_percent)

    for position, image_index in enumerate(indexes, start=1):
        before_percent = start_percent + int((position - 1) * percent_range / total_indexes)
        write_stage(
            percent=before_percent,
            message=f"{label}: mounting index {image_index}...",
            on_progress=on_progress,
        )

        clear_directory(mount_dir)
        mounted = False
        try:
            run_command(
                "dism.exe",
                ["/Mount-Image", f"/ImageFile:{wim_path}", f"/Index:{image_index}", f"/MountDir:{mount_dir}"],
                error_message=f"{label} index {image_index} mount failed",
                on_output_line=lambda line: write_command_output(
                    line=line,
                    percent=before_percent,
                    prefix=f"{label}[{image_index}] ",
                    on_progress=on_progress,
                ),
            )
            mounted = True

            before_drivers = get_image_third_party_drivers(mount_dir)
            before_published_names = {item.published_name.lower() for item in before_drivers if item.published_name}
            write_stage(
                percent=before_percent,
                message=f"{label}[{image_index}] third-party drivers before add: {len(before_drivers)}",
                on_progress=on_progress,
            )

            write_stage(
                percent=before_percent,
                message=f"{label}: injecting drivers into index {image_index}...",
                on_progress=on_progress,
            )

            run_command(
                "dism.exe",
                [f"/Image:{mount_dir}", "/Add-Driver", f"/Driver:{drivers_path}", "/Recurse"],
                error_message=f"{label} index {image_index} driver injection failed",
                on_output_line=lambda line: write_command_output(
                    line=line,
                    percent=before_percent,
                    prefix=f"{label}[{image_index}] ",
                    on_progress=on_progress,
                ),
            )

            after_drivers = get_image_third_party_drivers(mount_dir)
            added_drivers = [
                item
                for item in after_drivers
                if item.published_name and item.published_name.lower() not in before_published_names
            ]
            unique_added_by_published: dict[str, ThirdPartyDriver] = {}
            for driver in added_drivers:
                unique_added_by_published[driver.published_name.lower()] = driver
            unique_added = sorted(
                unique_added_by_published.values(),
                key=lambda item: (item.original_file_name.lower(), item.provider_name.lower(), item.published_name.lower()),
            )

            write_stage(
                percent=before_percent,
                message=f"{label}[{image_index}] third-party drivers after add: {len(after_drivers)}",
                on_progress=on_progress,
            )
            write_stage(
                percent=before_percent,
                message=f"{label}[{image_index}] newly added drivers: {len(unique_added)}",
                on_progress=on_progress,
            )
            for driver in unique_added:
                original_name = driver.original_file_name or "<unknown>"
                provider_name = driver.provider_name or "<unknown>"
                class_name = driver.class_name or "<unknown>"
                write_stage(
                    percent=before_percent,
                    message=(
                        f"{label}[{image_index}] added driver: {original_name} | "
                        f"Provider={provider_name} | Class={class_name} | Published={driver.published_name}"
                    ),
                    on_progress=on_progress,
                )

            if required_lookup:
                matched_required = sorted(
                    {
                        driver.original_file_name.strip()
                        for driver in after_drivers
                        if driver.original_file_name and driver.original_file_name.strip().lower() in required_lookup
                    }
                )
                write_stage(
                    percent=before_percent,
                    message=(
                        f"{label}[{image_index}] detected {len(matched_required)} "
                        f"{required_original_inf_label} driver(s) in mounted image."
                    ),
                    on_progress=on_progress,
                )
                for matched_inf in matched_required:
                    write_stage(
                        percent=before_percent,
                        message=f"{label}[{image_index}] confirmed {required_original_inf_label} driver: {matched_inf}",
                        on_progress=on_progress,
                    )

                if not matched_required:
                    expected = required_inf_names[:12]
                    expected_text = ", ".join(expected)
                    if len(required_inf_names) > len(expected):
                        expected_text += ", ..."
                    raise RuntimeError(
                        f"{label} index {image_index} does not contain any {required_original_inf_label} driver after "
                        f"injection. Expected one of: {expected_text}"
                    )

            write_stage(
                percent=before_percent,
                message=f"{label}: committing index {image_index}...",
                on_progress=on_progress,
            )
            run_command(
                "dism.exe",
                ["/Unmount-Image", f"/MountDir:{mount_dir}", "/Commit"],
                error_message=f"{label} index {image_index} commit failed",
                on_output_line=lambda line: write_command_output(
                    line=line,
                    percent=before_percent,
                    prefix=f"{label}[{image_index}] ",
                    on_progress=on_progress,
                ),
            )
            mounted = False
        except Exception:
            if mounted:
                try:
                    run_command(
                        "dism.exe",
                        ["/Unmount-Image", f"/MountDir:{mount_dir}", "/Discard"],
                        error_message=f"{label} cleanup discard failed",
                        on_output_line=lambda line: write_command_output(
                            line=line,
                            percent=before_percent,
                            prefix=f"{label}[{image_index}] ",
                            on_progress=on_progress,
                        ),
                    )
                except Exception:
                    pass
            raise

        after_percent = start_percent + int(position * percent_range / total_indexes)
        write_stage(
            percent=after_percent,
            message=f"{label}: index {image_index} complete.",
            on_progress=on_progress,
        )


def convert_install_esd_to_wim(
    *,
    esd_path: str,
    wim_path: str,
    start_percent: int,
    end_percent: int,
    on_progress: callable | None,
) -> None:
    wim_file = Path(wim_path)
    if wim_file.exists():
        wim_file.unlink()

    esd_indexes = get_wim_indexes(esd_path)
    total_indexes = max(1, len(esd_indexes))
    percent_range = max(1, end_percent - start_percent)

    for position, image_index in enumerate(esd_indexes, start=1):
        before_percent = start_percent + int((position - 1) * percent_range / total_indexes)
        write_stage(
            percent=before_percent,
            message=f"Converting install.esd index {image_index} to install.wim...",
            on_progress=on_progress,
        )

        run_command(
            "dism.exe",
            [
                "/Export-Image",
                f"/SourceImageFile:{esd_path}",
                f"/SourceIndex:{image_index}",
                f"/DestinationImageFile:{wim_path}",
                "/Compress:max",
                "/CheckIntegrity",
            ],
            error_message=f"Failed to convert install.esd index {image_index} to install.wim",
            on_output_line=lambda line: write_command_output(
                line=line,
                percent=before_percent,
                prefix=f"install.wim[{image_index}] ",
                on_progress=on_progress,
            ),
        )

        after_percent = start_percent + int(position * percent_range / total_indexes)
        write_stage(
            percent=after_percent,
            message=f"Converted install.esd index {image_index}.",
            on_progress=on_progress,
        )


def get_usb_drive_options() -> list[dict[str, str]]:
    command = """
    $items = @()
    Get-Partition | Where-Object { $_.DriveLetter } | Sort-Object DriveLetter | ForEach-Object {
        try {
            $disk = Get-Disk -Number $_.DiskNumber -ErrorAction Stop
            if ($disk.BusType -ne 'USB') { return }
            $volume = Get-Volume -DriveLetter $_.DriveLetter -ErrorAction SilentlyContinue
            $label = if ($volume -and $volume.FileSystemLabel) { $volume.FileSystemLabel } else { 'NoLabel' }
            $model = if ($disk.FriendlyName) { $disk.FriendlyName } else { 'USB Drive' }
            $items += [PSCustomObject]@{
                DriveLetter = [string]$_.DriveLetter
                SizeGB = [Math]::Round(($_.Size / 1GB), 1)
                Label = $label
                Model = $model
            }
        }
        catch {}
    }
    $items | ConvertTo-Json -Compress
    """

    result = run_powershell(command, error_message="Failed to enumerate USB drives")
    parsed = parse_json_output(result.output)

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    options: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        drive_letter = str(item.get("DriveLetter", "")).strip().upper()
        if not drive_letter:
            continue
        size_gb = item.get("SizeGB", "")
        label = str(item.get("Label", "NoLabel")).strip() or "NoLabel"
        model = str(item.get("Model", "USB Drive")).strip() or "USB Drive"
        display = f"{drive_letter}: ({size_gb} GB, {label}, {model})"
        options.append({"display": display, "drive_letter": drive_letter})

    return sorted(options, key=lambda item: item["drive_letter"])


def ensure_usb_drive(drive_letter: str) -> None:
    command = (
        f"$partition = Get-Partition -DriveLetter {ps_quote(drive_letter)} -ErrorAction Stop; "
        "$disk = Get-Disk -Number $partition.DiskNumber -ErrorAction Stop; "
        "[PSCustomObject]@{ BusType=[string]$disk.BusType } | ConvertTo-Json -Compress"
    )
    result = run_powershell(command, error_message=f"Failed to validate target drive {drive_letter}:")
    payload = parse_json_output(result.output)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unable to determine bus type for drive {drive_letter}:")
    bus_type = str(payload.get("BusType", "")).strip().upper()
    if bus_type != "USB":
        raise RuntimeError(f"Drive {drive_letter}: is not detected as a USB disk (BusType: {bus_type or 'Unknown'}).")


def mount_iso(iso_path: str) -> str:
    run_powershell(
        f"Mount-DiskImage -ImagePath {ps_quote(iso_path)} -ErrorAction Stop | Out-Null",
        error_message="Failed to mount ISO image",
    )

    letter_result = run_powershell(
        f"(Get-DiskImage -ImagePath {ps_quote(iso_path)} | Get-Volume | Select-Object -First 1 -ExpandProperty DriveLetter)",
        error_message="Failed to determine mounted ISO drive letter",
    )
    iso_drive_letter = ""
    for line in letter_result.output:
        if line and line.strip():
            iso_drive_letter = line.strip().upper()
            break
    if not iso_drive_letter:
        raise RuntimeError("Failed to determine mounted ISO drive letter.")
    return iso_drive_letter


def dismount_iso(iso_path: str) -> None:
    run_powershell(
        f"Dismount-DiskImage -ImagePath {ps_quote(iso_path)} -ErrorAction SilentlyContinue | Out-Null",
        error_message="Failed to dismount ISO image",
    )


def export_online_drivers(*, destination_root: Path, on_progress: callable | None, percent: int) -> Path:
    export_path = destination_root / "online_exported_drivers"
    export_path.mkdir(parents=True, exist_ok=True)
    write_stage(
        percent=percent,
        message=f"Exporting installed drivers from current Windows into: {export_path}",
        on_progress=on_progress,
    )
    run_command(
        "dism.exe",
        ["/Online", "/Export-Driver", f"/Destination:{export_path}"],
        error_message="Failed to export currently installed drivers from this system",
        on_output_line=lambda line: write_command_output(
            line=line,
            percent=percent,
            prefix="Online export: ",
            on_progress=on_progress,
        ),
    )
    return export_path


def invoke_usb_creation(
    *,
    iso_path: str,
    drivers_path: str,
    drive_letter: str,
    on_progress: callable | None,
) -> None:
    target_drive = drive_letter.strip().upper().rstrip(":")
    destination_root = f"{target_drive}:\\"
    mounted_iso = False
    extracted_drivers_root: Path | None = None
    extracted_installers_root: Path | None = None
    exported_online_drivers_root: Path | None = None
    iso_file = Path(iso_path)
    work_root = Path(tempfile.mkdtemp(prefix="MateBookUsbCreator_"))
    mount_dir = work_root / "Mount"
    mount_dir.mkdir(parents=True, exist_ok=True)
    log_info(
        "USB creation started. "
        f"iso_path={iso_path} drivers_path={drivers_path} target_drive={target_drive}: work_root={work_root}"
    )

    try:
        write_stage(percent=2, message="Validating inputs...", on_progress=on_progress)

        drivers_root = Path(drivers_path)
        effective_drivers_root = drivers_root
        if not iso_file.exists():
            raise RuntimeError(f"ISO file not found: {iso_path}")
        if not drivers_root.exists():
            raise RuntimeError(f"Driver folder not found: {drivers_path}")
        if target_drive == os.environ.get("SystemDrive", "C:").rstrip(":").upper():
            raise RuntimeError("Selected drive is the system drive. Select a USB drive.")

        repository_packages = get_driver_repository_inf_files(str(effective_drivers_root))
        if not repository_packages:
            extracted_drivers_root = extract_driver_archives(
                drivers_root=drivers_root,
                on_progress=on_progress,
                percent=3,
            )
            effective_drivers_root = extracted_drivers_root
            repository_packages = get_driver_repository_inf_files(str(effective_drivers_root))

        if not repository_packages:
            exe_count = len(
                [
                    path
                    for path in effective_drivers_root.rglob("*")
                    if path.is_file() and path.suffix.lower() == ".exe"
                ]
            )
            if exe_count > 0:
                seven_zip_path = find_7zip_executable()
                if seven_zip_path:
                    extracted_installers_root = Path(tempfile.mkdtemp(prefix="MateBookDriverExeExtract_"))
                    effective_drivers_root = extract_driver_installers_with_7zip(
                        source_root=effective_drivers_root,
                        destination_root=extracted_installers_root,
                        seven_zip_path=seven_zip_path,
                        on_progress=on_progress,
                        percent=4,
                    )
                    repository_packages = get_driver_repository_inf_files(str(effective_drivers_root))
                else:
                    write_stage(
                        percent=4,
                        message=(
                            "Driver packages are installer EXEs and 7-Zip was not found. "
                            "Falling back to exporting installed drivers from this Windows system..."
                        ),
                        on_progress=on_progress,
                    )
                    log_info("7-Zip not found; proceeding with online driver export fallback.")

        if not repository_packages:
            exported_online_drivers_root = Path(tempfile.mkdtemp(prefix="MateBookOnlineDriverExport_"))
            effective_drivers_root = export_online_drivers(
                destination_root=exported_online_drivers_root,
                on_progress=on_progress,
                percent=5,
            )
            repository_packages = get_driver_repository_inf_files(str(effective_drivers_root))

        if not repository_packages:
            extension_summary = summarize_driver_extensions(drivers_root)
            raise RuntimeError(
                f"No .inf driver packages found under: {drivers_path}. "
                f"Top file types: {extension_summary}"
            )

        wifi_packages = get_wifi_driver_packages(repository_packages)
        if not wifi_packages:
            wifi_inf_file_names = []
            write_stage(
                percent=5,
                message=(
                    "No Wi-Fi INF package was positively identified. "
                    "Continuing with full exported INF driver set."
                ),
                on_progress=on_progress,
            )
            log_info(
                "No Wi-Fi INF package matched detection rules. "
                "Proceeding without Wi-Fi-specific post-injection verification."
            )
        else:
            wifi_inf_file_names = sorted({item.file_name for item in wifi_packages})
            write_stage(percent=5, message=f"Detected {len(wifi_inf_file_names)} Wi-Fi INF package(s).", on_progress=on_progress)
            for package in wifi_packages:
                write_stage(percent=5, message=f"Wi-Fi package: {package.relative_path}", on_progress=on_progress)

        ensure_usb_drive(target_drive)

        write_stage(percent=10, message=f"Formatting {target_drive}: as NTFS...", on_progress=on_progress)
        run_powershell(
            (
                f"Format-Volume -DriveLetter {ps_quote(target_drive)} "
                "-FileSystem NTFS -NewFileSystemLabel 'WIN11USB' -Confirm:$false -Force | Out-Null"
            ),
            error_message=f"Failed to format drive {target_drive}:",
        )

        write_stage(percent=18, message="Mounting ISO...", on_progress=on_progress)
        iso_drive_letter = mount_iso(str(iso_file))
        mounted_iso = True
        iso_root = f"{iso_drive_letter}:\\"

        write_stage(percent=25, message="Copying Windows setup files to USB...", on_progress=on_progress)
        run_command(
            "robocopy.exe",
            [iso_root, destination_root, "*.*", "/E", "/R:1", "/W:1", "/COPY:DAT", "/DCOPY:DAT", "/TEE", "/NP"],
            error_message="Failed to copy ISO files to USB",
            accept_exit_codes={0, 1, 2, 3, 4, 5, 6, 7},
            on_output_line=lambda line: write_command_output(
                line=line,
                percent=25,
                prefix="ISO copy: ",
                on_progress=on_progress,
                robocopy_only=True,
            ),
        )

        write_stage(percent=45, message="Copying INF-ready drivers to USB...", on_progress=on_progress)
        driver_destination = Path(destination_root) / "MateBook-Drivers"
        if driver_destination.exists():
            shutil.rmtree(driver_destination, ignore_errors=True)
        driver_destination.mkdir(parents=True, exist_ok=True)

        run_command(
            "robocopy.exe",
            [
                str(effective_drivers_root),
                str(driver_destination),
                "*.*",
                "/E",
                "/R:1",
                "/W:1",
                "/COPY:DAT",
                "/DCOPY:DAT",
                "/TEE",
                "/NP",
            ],
            error_message="Failed to copy INF-ready drivers to USB",
            accept_exit_codes={0, 1, 2, 3, 4, 5, 6, 7},
            on_output_line=lambda line: write_command_output(
                line=line,
                percent=45,
                prefix="INF driver copy: ",
                on_progress=on_progress,
                robocopy_only=True,
            ),
        )

        try:
            source_is_same = effective_drivers_root.resolve().samefile(drivers_root.resolve())
        except Exception:
            source_is_same = str(effective_drivers_root).lower() == str(drivers_root).lower()

        if not source_is_same:
            write_stage(
                percent=48,
                message="Copying original driver package source to USB...",
                on_progress=on_progress,
            )
            original_source_destination = Path(destination_root) / "MateBook-Drivers-Source"
            if original_source_destination.exists():
                shutil.rmtree(original_source_destination, ignore_errors=True)
            original_source_destination.mkdir(parents=True, exist_ok=True)

            run_command(
                "robocopy.exe",
                [str(drivers_root), str(original_source_destination), "*.*", "/E", "/R:1", "/W:1", "/COPY:DAT", "/DCOPY:DAT", "/TEE", "/NP"],
                error_message="Failed to copy original driver package source to USB",
                accept_exit_codes={0, 1, 2, 3, 4, 5, 6, 7},
                on_output_line=lambda line: write_command_output(
                    line=line,
                    percent=48,
                    prefix="Source package copy: ",
                    on_progress=on_progress,
                    robocopy_only=True,
                ),
            )

        write_stage(
            percent=49,
            message="Driver load path during Windows Setup: USB\\MateBook-Drivers",
            on_progress=on_progress,
        )

        sources_path = Path(destination_root) / "sources"
        boot_wim = sources_path / "boot.wim"
        install_wim = sources_path / "install.wim"
        install_esd = sources_path / "install.esd"

        clear_readonly_attributes(
            paths=[boot_wim, install_wim, install_esd],
            on_progress=on_progress,
            percent=52,
        )

        if not boot_wim.exists():
            raise RuntimeError(f"boot.wim not found on USB at {boot_wim}")

        if not install_wim.exists():
            if install_esd.exists():
                write_stage(
                    percent=55,
                    message="install.esd detected. Converting to install.wim...",
                    on_progress=on_progress,
                )
                convert_install_esd_to_wim(
                    esd_path=str(install_esd),
                    wim_path=str(install_wim),
                    start_percent=55,
                    end_percent=70,
                    on_progress=on_progress,
                )
                clear_readonly_attributes(
                    paths=[install_wim],
                    on_progress=on_progress,
                    percent=70,
                )
            else:
                raise RuntimeError(f"Neither install.wim nor install.esd found under {sources_path}")
        else:
            write_stage(percent=65, message="Found install.wim.", on_progress=on_progress)

        boot_indexes = get_wim_indexes(str(boot_wim))
        boot_target_index = 2 if 2 in boot_indexes else boot_indexes[0]
        write_stage(
            percent=70,
            message=f"Injecting drivers into boot.wim index {boot_target_index}...",
            on_progress=on_progress,
        )
        add_drivers_to_wim(
            wim_path=str(boot_wim),
            indexes=[boot_target_index],
            drivers_path=str(effective_drivers_root),
            mount_dir=str(mount_dir),
            start_percent=70,
            end_percent=80,
            label="boot.wim",
            required_original_inf_names=wifi_inf_file_names,
            required_original_inf_label="Wi-Fi",
            on_progress=on_progress,
        )

        install_indexes = get_wim_indexes(str(install_wim))
        write_stage(
            percent=80,
            message=f"Injecting drivers into install.wim indexes: {', '.join(str(i) for i in install_indexes)}",
            on_progress=on_progress,
        )
        add_drivers_to_wim(
            wim_path=str(install_wim),
            indexes=install_indexes,
            drivers_path=str(effective_drivers_root),
            mount_dir=str(mount_dir),
            start_percent=80,
            end_percent=97,
            label="install.wim",
            required_original_inf_names=wifi_inf_file_names,
            required_original_inf_label="Wi-Fi",
            on_progress=on_progress,
        )

        bootsect_path = Path(iso_root) / "boot" / "bootsect.exe"
        if bootsect_path.exists():
            write_stage(percent=97, message="Applying BIOS boot code...", on_progress=on_progress)
            run_command(
                str(bootsect_path),
                ["/nt60", f"{target_drive}:", "/mbr"],
                error_message="bootsect failed",
                on_output_line=lambda line: write_command_output(
                    line=line,
                    percent=97,
                    prefix="bootsect: ",
                    on_progress=on_progress,
                ),
            )

        write_stage(percent=100, message="Completed successfully.", on_progress=on_progress)
        log_info("USB creation completed successfully.")
    finally:
        if mounted_iso:
            try:
                dismount_iso(str(iso_file))
            except Exception:
                pass

        try:
            run_command("dism.exe", ["/Cleanup-Wim"], error_message="DISM cleanup failed")
        except Exception:
            pass

        if extracted_drivers_root and extracted_drivers_root.exists():
            shutil.rmtree(extracted_drivers_root, ignore_errors=True)
        if extracted_installers_root and extracted_installers_root.exists():
            shutil.rmtree(extracted_installers_root, ignore_errors=True)
        if exported_online_drivers_root and exported_online_drivers_root.exists():
            shutil.rmtree(exported_online_drivers_root, ignore_errors=True)

        shutil.rmtree(work_root, ignore_errors=True)
        log_info("USB creation cleanup finished.")


class MateBookUsbCreatorApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("MateBook Win11 USB Creator (Python)")
        self.root.geometry("900x670")
        self.root.minsize(900, 670)

        self.is_running = False
        self.ui_queue: queue.Queue = queue.Queue()
        self.usb_display_to_letter: dict[str, str] = {}

        self.iso_var = StringVar(value=DEFAULT_ISO_PATH)
        self.drivers_var = StringVar(value=DEFAULT_DRIVERS_PATH)
        self.usb_var = StringVar(value="")
        self.status_var = StringVar(value="Ready")

        self._build_ui()
        self.refresh_usb_list()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._process_ui_queue)

    def _build_ui(self) -> None:
        content = ttk.Frame(self.root, padding=12)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(9, weight=1)

        ttk.Label(content, text="Windows 11 ISO").grid(row=0, column=0, sticky="w")
        iso_row = ttk.Frame(content)
        iso_row.grid(row=1, column=0, sticky="ew", pady=(2, 10))
        iso_row.columnconfigure(0, weight=1)
        self.txt_iso = ttk.Entry(iso_row, textvariable=self.iso_var)
        self.txt_iso.grid(row=0, column=0, sticky="ew")
        self.btn_browse_iso = ttk.Button(iso_row, text="Browse...", command=self.browse_iso)
        self.btn_browse_iso.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(content, text="Driver Folder (root)").grid(row=2, column=0, sticky="w")
        driver_row = ttk.Frame(content)
        driver_row.grid(row=3, column=0, sticky="ew", pady=(2, 10))
        driver_row.columnconfigure(0, weight=1)
        self.txt_drivers = ttk.Entry(driver_row, textvariable=self.drivers_var)
        self.txt_drivers.grid(row=0, column=0, sticky="ew")
        self.btn_browse_drivers = ttk.Button(driver_row, text="Browse...", command=self.browse_drivers)
        self.btn_browse_drivers.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(content, text="USB Target (will be formatted NTFS)").grid(row=4, column=0, sticky="w")
        usb_row = ttk.Frame(content)
        usb_row.grid(row=5, column=0, sticky="ew", pady=(2, 10))
        usb_row.columnconfigure(0, weight=1)
        self.cmb_usb = ttk.Combobox(usb_row, textvariable=self.usb_var, state="readonly")
        self.cmb_usb.grid(row=0, column=0, sticky="ew")
        self.btn_refresh_usb = ttk.Button(usb_row, text="Refresh", command=self.refresh_usb_list)
        self.btn_refresh_usb.grid(row=0, column=1, padx=(8, 0))

        self.btn_start = ttk.Button(content, text="Create Bootable USB", command=self.start_creation)
        self.btn_start.grid(row=6, column=0, sticky="ew")

        self.progress = ttk.Progressbar(content, mode="determinate", maximum=100)
        self.progress.grid(row=7, column=0, sticky="ew", pady=(10, 6))

        self.lbl_status = ttk.Label(content, textvariable=self.status_var)
        self.lbl_status.grid(row=8, column=0, sticky="w", pady=(0, 6))

        self.txt_log = ScrolledText(content, wrap="none", height=18)
        self.txt_log.grid(row=9, column=0, sticky="nsew")
        self.txt_log.configure(state="disabled")

    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{timestamp}] {message}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def browse_iso(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Select Windows 11 ISO",
            filetypes=[("ISO files", "*.iso"), ("All files", "*.*")],
        )
        if file_path:
            self.iso_var.set(file_path)

    def browse_drivers(self) -> None:
        initial_dir = self.drivers_var.get().strip() or os.getcwd()
        folder = filedialog.askdirectory(title="Select Driver Folder", initialdir=initial_dir)
        if folder:
            self.drivers_var.set(folder)

    def refresh_usb_list(self) -> None:
        previous = self.usb_var.get().strip()
        try:
            options = get_usb_drive_options()
        except Exception as exc:
            messagebox.showerror("USB detection error", str(exc))
            return

        displays = [item["display"] for item in options]
        self.usb_display_to_letter = {item["display"]: item["drive_letter"] for item in options}
        self.cmb_usb["values"] = displays

        if not displays:
            self.usb_var.set("")
            return

        selected = ""
        if previous in displays:
            selected = previous
        if not selected:
            for display in displays:
                if display.startswith(f"{DEFAULT_USB_LETTER}:"):
                    selected = display
                    break
        if not selected:
            selected = displays[0]
        self.usb_var.set(selected)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        self.btn_start.configure(state=state)
        self.btn_browse_iso.configure(state=state)
        self.btn_browse_drivers.configure(state=state)
        self.btn_refresh_usb.configure(state=state)
        self.cmb_usb.configure(state=combo_state)

    def start_creation(self) -> None:
        if self.is_running:
            return

        iso_path = self.iso_var.get().strip()
        drivers_path = self.drivers_var.get().strip()
        selected_display = self.usb_var.get().strip()
        drive_letter = self.usb_display_to_letter.get(selected_display, "")

        if not Path(iso_path).exists():
            messagebox.showwarning("Validation error", "ISO file not found.")
            return
        if not Path(drivers_path).exists():
            messagebox.showwarning("Validation error", "Driver folder not found.")
            return
        if not drive_letter:
            messagebox.showwarning("Validation error", "Select a USB target.")
            return

        confirm = messagebox.askyesno("Confirm format", f"This will erase all data on {drive_letter}:. Continue?")
        if not confirm:
            return

        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")
        self.progress["value"] = 0
        self.status_var.set("Starting...")
        self._set_controls_enabled(False)
        self.is_running = True

        worker = threading.Thread(
            target=self._run_creation_worker,
            kwargs={
                "iso_path": iso_path,
                "drivers_path": drivers_path,
                "drive_letter": drive_letter,
            },
            daemon=True,
        )
        worker.start()

    def _run_creation_worker(self, *, iso_path: str, drivers_path: str, drive_letter: str) -> None:
        def progress_callback(percent: int, message: str) -> None:
            self.ui_queue.put(("progress", int(percent), str(message)))

        try:
            log_info(
                "Worker thread started. "
                f"iso_path={iso_path} drivers_path={drivers_path} drive_letter={drive_letter}"
            )
            invoke_usb_creation(
                iso_path=iso_path,
                drivers_path=drivers_path,
                drive_letter=drive_letter,
                on_progress=progress_callback,
            )
            log_info("Worker thread finished successfully.")
            self.ui_queue.put(("success",))
        except Exception as exc:
            log_exception("Worker thread failed.")
            self.ui_queue.put(("error", str(exc), traceback.format_exc()))
        finally:
            self.ui_queue.put(("complete",))

    def _process_ui_queue(self) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            event_type = event[0]
            if event_type == "progress":
                _, percent, message = event
                self.progress["value"] = max(0, min(100, int(percent)))
                self.status_var.set(message)
                self.append_log(message)
            elif event_type == "success":
                self.status_var.set("Done")
                messagebox.showinfo("MateBook Win11 USB Creator", "Bootable USB has been created successfully.")
            elif event_type == "error":
                _, message, trace = event
                self.status_var.set("Failed")
                self.append_log("")
                self.append_log(f"ERROR: {message}")
                self.append_log("Traceback:")
                for trace_line in trace.splitlines():
                    self.append_log(trace_line)
                messagebox.showerror("MateBook Win11 USB Creator", f"USB creation failed.\n\n{message}")
            elif event_type == "complete":
                self._set_controls_enabled(True)
                self.is_running = False

        self.root.after(100, self._process_ui_queue)

    def on_close(self) -> None:
        if self.is_running:
            messagebox.showinfo(
                "Operation in progress",
                "USB creation is currently running. Wait for completion before closing.",
            )
            return
        self.root.destroy()


def main() -> int:
    configure_logging()
    log_info(f"Process start. pid={os.getpid()} argv={sys.argv!r} cwd={os.getcwd()} exe={sys.executable}")

    if os.name != "nt":
        log_info("Unsupported OS. Windows is required.")
        print("This tool is supported on Windows only.", file=sys.stderr)
        return 1

    if TK_IMPORT_ERROR is not None:
        log_exception("Tkinter import failed.")
        show_startup_error(
            "Python Tkinter is not available.\n\n"
            f"Install Tk support for your Python environment.\n\nDetails: {TK_IMPORT_ERROR}\n\n"
            f"Log file: {LOG_PATH}"
        )
        return 1

    admin_state = is_user_admin()
    log_info(f"Administrative privileges detected: {admin_state}")
    if not admin_state:
        try:
            relaunch_as_admin()
        except Exception as exc:
            log_exception("Elevation request failed.")
            print(str(exc), file=sys.stderr)
            return 1
        log_info("Elevation requested successfully. Exiting unelevated parent process.")
        return 0

    try:
        root = Tk()
        MateBookUsbCreatorApp(root)
        log_info("GUI created successfully. Entering Tk main loop.")
        root.mainloop()
        log_info("Tk main loop exited normally.")
        return 0
    except Exception:
        log_exception("Fatal startup/runtime error in GUI.")
        show_startup_error(
            "MateBook Win11 USB Creator failed to start.\n\n"
            f"Check log file:\n{LOG_PATH}"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
