# matebook-pro-usb-creator

GUI PowerShell tool to create a Windows 11 bootable USB and inject MateBook drivers.

## Run

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
- Copies your driver repository to `USB:\MateBook-Drivers`.
- Injects drivers into `sources\boot.wim` and `sources\install.wim` (all indexes).
- Shows live progress and log output in the GUI.

Warning: target USB is fully erased.
