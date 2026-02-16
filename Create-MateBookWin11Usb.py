from __future__ import annotations

import ctypes
import json
import locale
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


DEFAULT_ISO_PATH = r"C:\Users\georg\Downloads\Win11_25H2_EnglishInternational_x64.iso"
DEFAULT_DRIVERS_PATH = r"D:\Matebook Drivers"
DEFAULT_USB_LETTER = "E"


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


def is_user_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def quote_arg(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def relaunch_as_admin() -> None:
    params = " ".join(quote_arg(arg) for arg in sys.argv)
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        params,
        None,
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
        raise CommandError(
            f"{error_message}\nExit code: <unavailable>\nCommand: {file_path} {' '.join(arguments)}\n{tail}"
        )

    if int(exit_code) not in accept_exit_codes:
        tail = "\n".join(output[-20:])
        raise CommandError(
            f"{error_message}\nExit code: {exit_code}\nCommand: {file_path} {' '.join(arguments)}\n{tail}"
        )

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
