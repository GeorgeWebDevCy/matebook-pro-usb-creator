# matebook-pro-usb-creator

GUI tool to create a Windows 11 bootable USB and inject MateBook drivers.

## Run

### Python GUI (recommended)

1. Open PowerShell.
2. Run:

```powershell
python .\Create-MateBookWin11Usb.py
```

The app auto-elevates to Administrator.
If the Python window closes unexpectedly, check `Create-MateBookWin11Usb.log` in this folder.

### PowerShell GUI (legacy)

1. Open PowerShell.
2. Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\Create-MateBookWin11Usb.ps1
```

The script auto-elevates to Administrator.

## Defaults prefilled in GUI

- ISO: `C:\Users\georg\Downloads\Win11_25H2_EnglishInternational_x64.iso`
- Drivers root: `D:\Matebook Drivers`
- USB preference: `E:`

## What it does

- Detects USB targets and lets you choose one.
- Formats target USB as `NTFS`.
- Copies all Windows setup files from ISO to USB.
- Copies INF-ready drivers (the actual set used for injection) to `USB:\MateBook-Drivers`.
- If driver source had to be transformed (zip/exe/export), original source is also copied to `USB:\MateBook-Drivers-Source`.
- Injects drivers into `sources\boot.wim` and `sources\install.wim` (all indexes).
- Shows live progress and log output in the GUI.

Driver source note:
- The injector needs `.inf` packages.
- If your folder only has Huawei ZIPs, the app auto-extracts ZIPs.
- If those ZIPs contain installer EXEs, the app tries `7-Zip` extraction and caches a portable copy under `tools\7zip-portable`.
- If no `.inf` packages are found after extraction, the app falls back to exporting installed drivers from the current Windows system.

Warning: target USB is fully erased.
