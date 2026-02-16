[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Stage {
    param(
        [Parameter(Mandatory)]
        [int]$Percent,
        [Parameter(Mandatory)]
        [string]$Message,
        [scriptblock]$OnProgress
    )

    if ($Percent -lt 0) {
        $Percent = 0
    }
    if ($Percent -gt 100) {
        $Percent = 100
    }

    if ($OnProgress) {
        & $OnProgress $Percent $Message
    }
}

function Invoke-ExternalCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory)]
        [string]$ErrorMessage,
        [int[]]$AcceptExitCodes = @(0)
    )

    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()

    try {
        $process = Start-Process -FilePath $FilePath `
            -ArgumentList $Arguments `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutFile `
            -RedirectStandardError $stderrFile

        $output = @()
        if (Test-Path -LiteralPath $stdoutFile) {
            $output += Get-Content -LiteralPath $stdoutFile -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $stderrFile) {
            $output += Get-Content -LiteralPath $stderrFile -ErrorAction SilentlyContinue
        }

        if ($AcceptExitCodes -notcontains $process.ExitCode) {
            $tail = ($output | Select-Object -Last 20) -join [Environment]::NewLine
            throw "$ErrorMessage`nExit code: $($process.ExitCode)`nCommand: $FilePath $($Arguments -join ' ')`n$tail"
        }

        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            Output   = $output
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-WimIndexes {
    param(
        [Parameter(Mandatory)]
        [string]$WimPath
    )

    if (-not (Test-Path -LiteralPath $WimPath)) {
        throw "WIM image not found: $WimPath"
    }

    $result = Invoke-ExternalCommand -FilePath "dism.exe" `
        -Arguments @("/English", "/Get-WimInfo", "/WimFile:$WimPath") `
        -ErrorMessage "Unable to read image indexes from $WimPath"

    $indexes = @()
    foreach ($line in $result.Output) {
        if ($line -match "^\s*Index\s*:\s*(\d+)") {
            $indexes += [int]$Matches[1]
        }
    }

    if ($indexes.Count -eq 0) {
        throw "No image indexes found in: $WimPath"
    }

    return $indexes
}

function Add-DriversToWim {
    param(
        [Parameter(Mandatory)]
        [string]$WimPath,
        [Parameter(Mandatory)]
        [int[]]$Indexes,
        [Parameter(Mandatory)]
        [string]$DriversPath,
        [Parameter(Mandatory)]
        [string]$MountDir,
        [Parameter(Mandatory)]
        [int]$StartPercent,
        [Parameter(Mandatory)]
        [int]$EndPercent,
        [Parameter(Mandatory)]
        [string]$Label,
        [scriptblock]$OnProgress
    )

    $count = [Math]::Max(1, $Indexes.Count)
    $range = [Math]::Max(1, $EndPercent - $StartPercent)
    $position = 0

    foreach ($index in $Indexes) {
        $position++
        $beforePercent = $StartPercent + [int](($position - 1) * $range / $count)
        Write-Stage -Percent $beforePercent -Message "${Label}: mounting index $index..." -OnProgress $OnProgress

        Get-ChildItem -LiteralPath $MountDir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

        $mounted = $false
        try {
            Invoke-ExternalCommand -FilePath "dism.exe" `
                -Arguments @("/Mount-Image", "/ImageFile:$WimPath", "/Index:$index", "/MountDir:$MountDir") `
                -ErrorMessage "$Label index $index mount failed" | Out-Null
            $mounted = $true

            Write-Stage -Percent $beforePercent -Message "${Label}: injecting drivers into index $index..." -OnProgress $OnProgress
            Invoke-ExternalCommand -FilePath "dism.exe" `
                -Arguments @("/Image:$MountDir", "/Add-Driver", "/Driver:$DriversPath", "/Recurse") `
                -ErrorMessage "$Label index $index driver injection failed" | Out-Null

            Write-Stage -Percent $beforePercent -Message "${Label}: committing index $index..." -OnProgress $OnProgress
            Invoke-ExternalCommand -FilePath "dism.exe" `
                -Arguments @("/Unmount-Image", "/MountDir:$MountDir", "/Commit") `
                -ErrorMessage "$Label index $index commit failed" | Out-Null
            $mounted = $false
        }
        catch {
            if ($mounted) {
                try {
                    Invoke-ExternalCommand -FilePath "dism.exe" `
                        -Arguments @("/Unmount-Image", "/MountDir:$MountDir", "/Discard") `
                        -ErrorMessage "$Label cleanup discard failed" | Out-Null
                }
                catch {
                    # Best effort cleanup only.
                }
            }
            throw
        }

        $afterPercent = $StartPercent + [int]($position * $range / $count)
        Write-Stage -Percent $afterPercent -Message "${Label}: index $index complete." -OnProgress $OnProgress
    }
}

function Convert-InstallEsdToWim {
    param(
        [Parameter(Mandatory)]
        [string]$EsdPath,
        [Parameter(Mandatory)]
        [string]$WimPath,
        [Parameter(Mandatory)]
        [int]$StartPercent,
        [Parameter(Mandatory)]
        [int]$EndPercent,
        [scriptblock]$OnProgress
    )

    if (Test-Path -LiteralPath $WimPath) {
        Remove-Item -LiteralPath $WimPath -Force
    }

    $esdIndexes = Get-WimIndexes -WimPath $EsdPath
    $count = [Math]::Max(1, $esdIndexes.Count)
    $range = [Math]::Max(1, $EndPercent - $StartPercent)
    $position = 0

    foreach ($index in $esdIndexes) {
        $position++
        $beforePercent = $StartPercent + [int](($position - 1) * $range / $count)
        Write-Stage -Percent $beforePercent -Message "Converting install.esd index $index to install.wim..." -OnProgress $OnProgress

        Invoke-ExternalCommand -FilePath "dism.exe" `
            -Arguments @("/Export-Image", "/SourceImageFile:$EsdPath", "/SourceIndex:$index", "/DestinationImageFile:$WimPath", "/Compress:max", "/CheckIntegrity") `
            -ErrorMessage "Failed to convert install.esd index $index to install.wim" | Out-Null

        $afterPercent = $StartPercent + [int]($position * $range / $count)
        Write-Stage -Percent $afterPercent -Message "Converted install.esd index $index." -OnProgress $OnProgress
    }
}

function Invoke-UsbCreation {
    param(
        [Parameter(Mandatory)]
        [string]$IsoPath,
        [Parameter(Mandatory)]
        [string]$DriversPath,
        [Parameter(Mandatory)]
        [string]$DriveLetter,
        [scriptblock]$OnProgress
    )

    $targetDrive = $DriveLetter.ToUpper().TrimEnd(":")
    $destinationRoot = "$targetDrive`:\"
    $mountedIso = $false
    $workRoot = Join-Path -Path $env:TEMP -ChildPath ("MateBookUsbCreator_" + [Guid]::NewGuid().ToString("N"))
    $mountDir = Join-Path -Path $workRoot -ChildPath "Mount"
    New-Item -ItemType Directory -Path $mountDir -Force | Out-Null

    try {
        Write-Stage -Percent 2 -Message "Validating inputs..." -OnProgress $OnProgress

        if (-not (Test-Path -LiteralPath $IsoPath)) {
            throw "ISO file not found: $IsoPath"
        }
        if (-not (Test-Path -LiteralPath $DriversPath)) {
            throw "Driver folder not found: $DriversPath"
        }
        if ($targetDrive -eq $env:SystemDrive.TrimEnd(":")) {
            throw "Selected drive is the system drive. Select a USB drive."
        }

        $partition = Get-Partition -DriveLetter $targetDrive -ErrorAction Stop
        $disk = Get-Disk -Number $partition.DiskNumber -ErrorAction Stop
        if ($disk.BusType -ne "USB") {
            throw "Drive $targetDrive`: is not detected as a USB disk (BusType: $($disk.BusType))."
        }

        Write-Stage -Percent 10 -Message "Formatting $targetDrive`: as NTFS..." -OnProgress $OnProgress
        Format-Volume -DriveLetter $targetDrive -FileSystem NTFS -NewFileSystemLabel "WIN11USB" -Confirm:$false -Force | Out-Null

        Write-Stage -Percent 18 -Message "Mounting ISO..." -OnProgress $OnProgress
        Mount-DiskImage -ImagePath $IsoPath -ErrorAction Stop | Out-Null
        $mountedIso = $true

        $isoDriveLetter = (Get-DiskImage -ImagePath $IsoPath | Get-Volume | Select-Object -First 1 -ExpandProperty DriveLetter)
        if (-not $isoDriveLetter) {
            throw "Failed to determine mounted ISO drive letter."
        }
        $isoRoot = "$isoDriveLetter`:\"

        Write-Stage -Percent 25 -Message "Copying Windows setup files to USB..." -OnProgress $OnProgress
        Invoke-ExternalCommand -FilePath "robocopy.exe" `
            -Arguments @($isoRoot, $destinationRoot, "*.*", "/E", "/R:1", "/W:1", "/COPY:DAT", "/DCOPY:DAT", "/NP", "/NFL", "/NDL", "/NJH", "/NJS") `
            -ErrorMessage "Failed to copy ISO files to USB" `
            -AcceptExitCodes @(0, 1, 2, 3, 4, 5, 6, 7) | Out-Null

        Write-Stage -Percent 45 -Message "Copying driver repository to USB..." -OnProgress $OnProgress
        $driverDestination = Join-Path -Path $destinationRoot -ChildPath "MateBook-Drivers"
        if (Test-Path -LiteralPath $driverDestination) {
            Remove-Item -LiteralPath $driverDestination -Recurse -Force
        }
        New-Item -ItemType Directory -Path $driverDestination -Force | Out-Null
        Invoke-ExternalCommand -FilePath "robocopy.exe" `
            -Arguments @($DriversPath, $driverDestination, "*.*", "/E", "/R:1", "/W:1", "/COPY:DAT", "/DCOPY:DAT", "/NP", "/NFL", "/NDL", "/NJH", "/NJS") `
            -ErrorMessage "Failed to copy drivers to USB" `
            -AcceptExitCodes @(0, 1, 2, 3, 4, 5, 6, 7) | Out-Null

        $sourcesPath = Join-Path -Path $destinationRoot -ChildPath "sources"
        $bootWim = Join-Path -Path $sourcesPath -ChildPath "boot.wim"
        $installWim = Join-Path -Path $sourcesPath -ChildPath "install.wim"
        $installEsd = Join-Path -Path $sourcesPath -ChildPath "install.esd"

        if (-not (Test-Path -LiteralPath $bootWim)) {
            throw "boot.wim not found on USB at $bootWim"
        }

        if (-not (Test-Path -LiteralPath $installWim)) {
            if (Test-Path -LiteralPath $installEsd) {
                Write-Stage -Percent 55 -Message "install.esd detected. Converting to install.wim..." -OnProgress $OnProgress
                Convert-InstallEsdToWim -EsdPath $installEsd -WimPath $installWim -StartPercent 55 -EndPercent 70 -OnProgress $OnProgress
            }
            else {
                throw "Neither install.wim nor install.esd found under $sourcesPath"
            }
        }
        else {
            Write-Stage -Percent 65 -Message "Found install.wim." -OnProgress $OnProgress
        }

        $bootIndexes = Get-WimIndexes -WimPath $bootWim
        $bootTargetIndex = if ($bootIndexes -contains 2) { 2 } else { $bootIndexes[0] }
        Write-Stage -Percent 70 -Message "Injecting drivers into boot.wim index $bootTargetIndex..." -OnProgress $OnProgress
        Add-DriversToWim -WimPath $bootWim -Indexes @($bootTargetIndex) -DriversPath $DriversPath -MountDir $mountDir -StartPercent 70 -EndPercent 80 -Label "boot.wim" -OnProgress $OnProgress

        $installIndexes = Get-WimIndexes -WimPath $installWim
        Write-Stage -Percent 80 -Message ("Injecting drivers into install.wim indexes: {0}" -f ($installIndexes -join ", ")) -OnProgress $OnProgress
        Add-DriversToWim -WimPath $installWim -Indexes $installIndexes -DriversPath $DriversPath -MountDir $mountDir -StartPercent 80 -EndPercent 97 -Label "install.wim" -OnProgress $OnProgress

        $bootsectPath = Join-Path -Path $isoRoot -ChildPath "boot\bootsect.exe"
        if (Test-Path -LiteralPath $bootsectPath) {
            Write-Stage -Percent 97 -Message "Applying BIOS boot code..." -OnProgress $OnProgress
            Invoke-ExternalCommand -FilePath $bootsectPath `
                -Arguments @("/nt60", "$targetDrive`:", "/mbr") `
                -ErrorMessage "bootsect failed" | Out-Null
        }

        Write-Stage -Percent 100 -Message "Completed successfully." -OnProgress $OnProgress
    }
    finally {
        if ($mountedIso) {
            try {
                Dismount-DiskImage -ImagePath $IsoPath -ErrorAction SilentlyContinue | Out-Null
            }
            catch {
                # Best effort unmount.
            }
        }

        try {
            Invoke-ExternalCommand -FilePath "dism.exe" -Arguments @("/Cleanup-Wim") -ErrorMessage "DISM cleanup failed" | Out-Null
        }
        catch {
            # Best effort cleanup.
        }

        if (Test-Path -LiteralPath $workRoot) {
            Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-UsbDriveOptions {
    $options = @()
    $partitions = Get-Partition | Where-Object { $_.DriveLetter } | Sort-Object DriveLetter

    foreach ($partition in $partitions) {
        try {
            $disk = Get-Disk -Number $partition.DiskNumber -ErrorAction Stop
            if ($disk.BusType -ne "USB") {
                continue
            }

            $volume = Get-Volume -DriveLetter $partition.DriveLetter -ErrorAction SilentlyContinue
            $sizeGb = [Math]::Round(($partition.Size / 1GB), 1)
            $label = if ($volume -and $volume.FileSystemLabel) { $volume.FileSystemLabel } else { "NoLabel" }
            $model = if ($disk.FriendlyName) { $disk.FriendlyName } else { "USB Drive" }

            $options += [PSCustomObject]@{
                Display    = "{0}: ({1} GB, {2}, {3})" -f $partition.DriveLetter, $sizeGb, $label, $model
                DriveLetter = [string]$partition.DriveLetter
            }
        }
        catch {
            # Skip partitions that cannot be queried cleanly.
        }
    }

    return $options
}

if (-not (Test-IsAdministrator)) {
    if (-not $PSCommandPath) {
        throw "Run this script from a saved .ps1 file."
    }

    $args = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    Start-Process -FilePath "powershell.exe" -ArgumentList $args -Verb RunAs | Out-Null
    exit
}

$form = [System.Windows.Forms.Form]::new()
$form.Text = "MateBook Win11 USB Creator"
$form.Size = [System.Drawing.Size]::new(900, 670)
$form.StartPosition = "CenterScreen"
$form.MinimumSize = [System.Drawing.Size]::new(900, 670)

$lblIso = [System.Windows.Forms.Label]::new()
$lblIso.Text = "Windows 11 ISO"
$lblIso.Location = [System.Drawing.Point]::new(20, 20)
$lblIso.AutoSize = $true
$form.Controls.Add($lblIso)

$txtIso = [System.Windows.Forms.TextBox]::new()
$txtIso.Location = [System.Drawing.Point]::new(20, 45)
$txtIso.Size = [System.Drawing.Size]::new(730, 24)
$txtIso.Text = "C:\Users\georg\Downloads\Win11_25H2_EnglishInternational_x64.iso"
$form.Controls.Add($txtIso)

$btnBrowseIso = [System.Windows.Forms.Button]::new()
$btnBrowseIso.Text = "Browse..."
$btnBrowseIso.Location = [System.Drawing.Point]::new(760, 43)
$btnBrowseIso.Size = [System.Drawing.Size]::new(110, 28)
$form.Controls.Add($btnBrowseIso)

$lblDrivers = [System.Windows.Forms.Label]::new()
$lblDrivers.Text = "Driver Folder (root)"
$lblDrivers.Location = [System.Drawing.Point]::new(20, 85)
$lblDrivers.AutoSize = $true
$form.Controls.Add($lblDrivers)

$txtDrivers = [System.Windows.Forms.TextBox]::new()
$txtDrivers.Location = [System.Drawing.Point]::new(20, 110)
$txtDrivers.Size = [System.Drawing.Size]::new(730, 24)
$txtDrivers.Text = "D:\Matebook Drivers"
$form.Controls.Add($txtDrivers)

$btnBrowseDrivers = [System.Windows.Forms.Button]::new()
$btnBrowseDrivers.Text = "Browse..."
$btnBrowseDrivers.Location = [System.Drawing.Point]::new(760, 108)
$btnBrowseDrivers.Size = [System.Drawing.Size]::new(110, 28)
$form.Controls.Add($btnBrowseDrivers)

$lblUsb = [System.Windows.Forms.Label]::new()
$lblUsb.Text = "USB Target (will be formatted NTFS)"
$lblUsb.Location = [System.Drawing.Point]::new(20, 150)
$lblUsb.AutoSize = $true
$form.Controls.Add($lblUsb)

$cmbUsb = [System.Windows.Forms.ComboBox]::new()
$cmbUsb.Location = [System.Drawing.Point]::new(20, 175)
$cmbUsb.Size = [System.Drawing.Size]::new(730, 24)
$cmbUsb.DropDownStyle = "DropDownList"
$cmbUsb.DisplayMember = "Display"
$form.Controls.Add($cmbUsb)

$btnRefreshUsb = [System.Windows.Forms.Button]::new()
$btnRefreshUsb.Text = "Refresh"
$btnRefreshUsb.Location = [System.Drawing.Point]::new(760, 173)
$btnRefreshUsb.Size = [System.Drawing.Size]::new(110, 28)
$form.Controls.Add($btnRefreshUsb)

$btnStart = [System.Windows.Forms.Button]::new()
$btnStart.Text = "Create Bootable USB"
$btnStart.Location = [System.Drawing.Point]::new(20, 220)
$btnStart.Size = [System.Drawing.Size]::new(850, 36)
$form.Controls.Add($btnStart)

$progressBar = [System.Windows.Forms.ProgressBar]::new()
$progressBar.Location = [System.Drawing.Point]::new(20, 272)
$progressBar.Size = [System.Drawing.Size]::new(850, 24)
$progressBar.Minimum = 0
$progressBar.Maximum = 100
$progressBar.Value = 0
$form.Controls.Add($progressBar)

$lblStatus = [System.Windows.Forms.Label]::new()
$lblStatus.Text = "Ready"
$lblStatus.Location = [System.Drawing.Point]::new(20, 305)
$lblStatus.AutoSize = $true
$form.Controls.Add($lblStatus)

$txtLog = [System.Windows.Forms.TextBox]::new()
$txtLog.Location = [System.Drawing.Point]::new(20, 330)
$txtLog.Size = [System.Drawing.Size]::new(850, 280)
$txtLog.Multiline = $true
$txtLog.ScrollBars = "Vertical"
$txtLog.ReadOnly = $true
$txtLog.WordWrap = $false
$form.Controls.Add($txtLog)

$openIsoDialog = [System.Windows.Forms.OpenFileDialog]::new()
$openIsoDialog.Filter = "ISO files (*.iso)|*.iso|All files (*.*)|*.*"
$openIsoDialog.CheckFileExists = $true
$openIsoDialog.Multiselect = $false

$folderDialog = [System.Windows.Forms.FolderBrowserDialog]::new()
$folderDialog.ShowNewFolderButton = $false

$script:isRunning = $false

function Refresh-UsbList {
    $previousLetter = $null
    if ($cmbUsb.SelectedItem -and $cmbUsb.SelectedItem.PSObject.Properties["DriveLetter"]) {
        $previousLetter = [string]$cmbUsb.SelectedItem.DriveLetter
    }

    $cmbUsb.Items.Clear()
    $options = Get-UsbDriveOptions

    foreach ($option in $options) {
        [void]$cmbUsb.Items.Add($option)
    }

    if ($cmbUsb.Items.Count -eq 0) {
        return
    }

    $selected = $null
    if ($previousLetter) {
        foreach ($item in $cmbUsb.Items) {
            if ($item.DriveLetter -eq $previousLetter) {
                $selected = $item
                break
            }
        }
    }

    if (-not $selected) {
        foreach ($item in $cmbUsb.Items) {
            if ($item.DriveLetter -eq "E") {
                $selected = $item
                break
            }
        }
    }

    if ($selected) {
        $cmbUsb.SelectedItem = $selected
    }
    else {
        $cmbUsb.SelectedIndex = 0
    }
}

$btnRefreshUsb.Add_Click({
        Refresh-UsbList
    })

$btnBrowseIso.Add_Click({
        if ($openIsoDialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $txtIso.Text = $openIsoDialog.FileName
        }
    })

$btnBrowseDrivers.Add_Click({
        if (Test-Path -LiteralPath $txtDrivers.Text) {
            $folderDialog.SelectedPath = $txtDrivers.Text
        }

        if ($folderDialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $txtDrivers.Text = $folderDialog.SelectedPath
        }
    })

function Update-UiProgress {
    param(
        [Parameter(Mandatory)]
        [int]$Percent,
        [Parameter(Mandatory)]
        [string]$Message
    )

    $progressBar.Value = [Math]::Max(0, [Math]::Min(100, $Percent))
    $timestamp = (Get-Date).ToString("HH:mm:ss")
    $txtLog.AppendText("[$timestamp] $Message`r`n")
    $txtLog.SelectionStart = $txtLog.TextLength
    $txtLog.ScrollToCaret()
    $lblStatus.Text = $Message
    [System.Windows.Forms.Application]::DoEvents()
}

$btnStart.Add_Click({
        if ($worker.IsBusy) {
            return
        }

        $isoPath = $txtIso.Text.Trim()
        $driversPath = $txtDrivers.Text.Trim()
        $selectedUsb = $cmbUsb.SelectedItem

        if (-not (Test-Path -LiteralPath $isoPath)) {
            [System.Windows.Forms.MessageBox]::Show("ISO file not found.", "Validation error", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
            return
        }
        if (-not (Test-Path -LiteralPath $driversPath)) {
            [System.Windows.Forms.MessageBox]::Show("Driver folder not found.", "Validation error", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
            return
        }
        if (-not $selectedUsb) {
            [System.Windows.Forms.MessageBox]::Show("Select a USB target.", "Validation error", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
            return
        }

        $driveLetter = [string]$selectedUsb.DriveLetter
        $confirm = [System.Windows.Forms.MessageBox]::Show(
            "This will erase all data on $driveLetter`:. Continue?",
            "Confirm format",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        )
        if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) {
            return
        }

        $txtLog.Clear()
        $progressBar.Value = 0
        $lblStatus.Text = "Starting..."

        $btnStart.Enabled = $false
        $btnBrowseIso.Enabled = $false
        $btnBrowseDrivers.Enabled = $false
        $btnRefreshUsb.Enabled = $false
        $cmbUsb.Enabled = $false

        $inputData = [PSCustomObject]@{
            IsoPath     = $isoPath
            DriversPath = $driversPath
            DriveLetter = $driveLetter
        }

        $worker.RunWorkerAsync($inputData)
    })

$form.add_FormClosing({
        if ($worker.IsBusy) {
            [System.Windows.Forms.MessageBox]::Show(
                "USB creation is currently running. Wait for completion before closing.",
                "Operation in progress",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Information
            ) | Out-Null
            $_.Cancel = $true
        }
    })

Refresh-UsbList
[void]$form.ShowDialog()
