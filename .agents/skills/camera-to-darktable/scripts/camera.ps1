param(
    [ValidateSet('Scan','Transfer')][string]$Action = 'Scan',
    [string]$CameraName = 'ILCE-7CR',
    [string]$CameraId,
    [string]$Output,
    [string]$Plan,
    [string]$Run,
    [int]$StallSeconds = 300
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'The MTP adapter requires Windows PowerShell 7' }
$timer = [Diagnostics.Stopwatch]::StartNew()
$shell = New-Object -ComObject Shell.Application
$devices = @($shell.Namespace(17).Items() | Where-Object { $_.Name -eq $CameraName })
if ($devices.Count -ne 1) { throw "Expected one connected $CameraName device, found $($devices.Count)" }
$pnp = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -eq $CameraName -and $_.PNPDeviceID -like 'USB\*' })
if (-not $CameraId) {
    if ($pnp.Count -ne 1) { throw 'A stable camera ID could not be resolved; pass -CameraId explicitly' }
    $CameraId = $pnp[0].PNPDeviceID.ToUpperInvariant()
}
$storages = @($devices[0].GetFolder.Items())
if ($storages.Count -ne 1) { throw 'Select a device with one storage or extend the adapter for multiple cards' }
$days = @($storages[0].GetFolder.Items())
$formats = @('.arw','.jpg','.jpeg','.dng','.tif','.tiff')

if ($Action -eq 'Scan') {
    if (-not $Output) { throw '-Output is required for Scan' }
    $files = [Collections.Generic.List[object]]::new()
    $excluded = [Collections.Generic.List[string]]::new()
    foreach ($day in $days) {
        if (-not $day.IsFolder -or $day.Name -notmatch '^\d{4}-\d{2}-\d{2}$') {
            throw "Camera date-folder layout is unsupported: $($day.Name)"
        }
        foreach ($item in $day.GetFolder.Items()) {
            if ($item.IsFolder -or [IO.Path]::GetExtension($item.Name).ToLowerInvariant() -notin $formats) {
                $excluded.Add("$($day.Name)/$($item.Name)"); continue
            }
            $length = [long]$item.ExtendedProperty('System.Size')
            if ($length -le 0) { throw "Missing size: $($item.Name)" }
            $files.Add([ordered]@{source_path="$($day.Name)/$($item.Name)"; day=$day.Name; name=$item.Name; size=$length})
        }
    }
    $manifest = [ordered]@{schema_version=1; camera_id=$CameraId; camera_name=$CameraName;
        storage_name=$storages[0].Name; scanned_at=[DateTime]::UtcNow.ToString('o');
        scan_seconds=[Math]::Round($timer.Elapsed.TotalSeconds,2); files=@($files); excluded=@($excluded)}
    $parent = Split-Path -Parent $Output
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Output -Encoding utf8
    $totalScanBytes = 0L
    foreach ($file in $files) { $totalScanBytes += [long]$file.size }
    [ordered]@{files=$files.Count; bytes=$totalScanBytes;
        excluded=$excluded.Count; seconds=$manifest.scan_seconds; camera_id=$CameraId} | ConvertTo-Json -Compress
    exit
}

if (-not $Plan -or -not $Run) { throw '-Plan and -Run are required for Transfer' }
$request = Get-Content -LiteralPath $Plan -Raw | ConvertFrom-Json
if ($request.camera_id -ne $CameraId) { throw 'The connected camera does not match the plan' }
$pending = @($request.files | Where-Object { $_.action -eq 'copy' -or $_.action -eq 'conflict' })
$runRoot = [IO.Path]::GetFullPath($Run)
$stageRoot = Join-Path $runRoot 'staging'
if (-not (Test-Path -LiteralPath $stageRoot)) { New-Item -ItemType Directory -Path $stageRoot | Out-Null }
$readyRoot = Join-Path $runRoot 'ready'
if (-not (Test-Path -LiteralPath $readyRoot)) { New-Item -ItemType Directory -Path $readyRoot | Out-Null }
$batches = 0
foreach ($group in ($pending | Group-Object day)) {
    $sourceDays = @($days | Where-Object { $_.Name -eq $group.Name })
    if ($sourceDays.Count -ne 1) { throw "Source folder disappeared: $($group.Name)" }
    $folderItems = $sourceDays[0].GetFolder.Items()
    $actual = @($folderItems)
    foreach ($entry in $group.Group) {
        $matches = @($actual | Where-Object { $_.Name -eq $entry.name })
        if ($matches.Count -ne 1 -or [long]$matches[0].ExtendedProperty('System.Size') -ne [long]$entry.size) {
            throw "Source changed after planning: $($entry.source_path)"
        }
    }
    $local = Join-Path $stageRoot $group.Name
    if (-not [IO.Path]::GetFullPath($local).StartsWith($stageRoot + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Staging path escaped the run directory'
    }
    if (-not (Test-Path -LiteralPath $local)) { New-Item -ItemType Directory -Path $local | Out-Null }
    $wanted = @{}
    foreach ($entry in $group.Group) {
        $existingPath = Join-Path $local $entry.name
        if (Test-Path -LiteralPath $existingPath) {
            $existing = Get-Item -LiteralPath $existingPath
            if ($existing.Length -ne $entry.size) { throw "Partial staging exists; retain it and use a fresh run: $existingPath" }
            try {
                $handle = [IO.File]::Open($existingPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None)
                $handle.Dispose()
            } catch [IO.IOException] { throw "Staging is still in use; retry when the writer has closed it: $existingPath" }
        } else { $wanted[$entry.name] = $entry }
    }
    if ($wanted.Count -eq 0) { continue }
    $destination = $shell.Namespace($local)
    if (-not $destination) { throw 'Could not open staging destination' }
    if ($actual.Count -eq $wanted.Count -and @($actual | Where-Object { $_.IsFolder -or -not $wanted.ContainsKey($_.Name) }).Count -eq 0) {
        [Console]::WriteLine("copying folder {0}: {1} photos",$group.Name,$wanted.Count)
        $destination.CopyHere($folderItems,1556)
        $batches++
    } else {
        # Sony filters by its parsing ID, not the displayed photo name; verify the selected names before copying
        $filters = @{}
        foreach ($item in $actual) {
            if ($wanted.ContainsKey($item.Name)) { $filters[$item.Name] = [IO.Path]::GetFileName($item.Path) }
        }
        $selected = $sourceDays[0].GetFolder.Items()
        $selected.Filter(64,(@($wanted.Keys | ForEach-Object { $filters[$_] }) -join ';'))
        $selection = @($selected)
        if ($selection.Count -eq $wanted.Count -and @($selection | Where-Object { $_.IsFolder -or -not $wanted.ContainsKey($_.Name) }).Count -eq 0) {
            [Console]::WriteLine("copying batch {0}: {1} photos",$group.Name,$wanted.Count)
            $destination.CopyHere($selected,1556); $batches++
        } else {
            foreach ($name in $wanted.Keys) {
                $selected = $sourceDays[0].GetFolder.Items()
                $selected.Filter(64,$filters[$name])
                $selection = @($selected)
                if ($selection.Count -ne 1 -or $selection[0].Name -ne $name -or $selection[0].IsFolder) {
                    throw "The camera provider did not apply an exact filter: $name"
                }
                [Console]::WriteLine("copying {0}/{1}",$group.Name,$name)
                $destination.CopyHere($selected,1556); $batches++
            }
        }
    }
    Write-Output ("queued {0}: {1} photos" -f $group.Name,$wanted.Count)
}
$totalBytes = [long](($pending | Measure-Object size -Sum).Sum)
$lastProgress = [DateTime]::UtcNow
$previousBytes = -1L
$previousComplete = -1
$previousStable = @{}
do {
    $complete = 0; $transferred = 0L; $stable = @{}
    foreach ($entry in $pending) {
        $readyPath = Join-Path $readyRoot ($entry.key + '.ready')
        $path = Join-Path $stageRoot $entry.source_path.Replace('/',[IO.Path]::DirectorySeparatorChar)
        $file = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
        if ($file) {
            $transferred += [Math]::Min([long]$file.Length,[long]$entry.size)
            if (Test-Path -LiteralPath $readyPath) { $complete++; continue }
            if ($file.Length -eq $entry.size) {
                try {
                    $handle = [IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None)
                    $handle.Dispose()
                    $stable[$entry.source_path] = $file.LastWriteTimeUtc.Ticks
                    if ($previousStable.ContainsKey($entry.source_path) -and $previousStable[$entry.source_path] -eq $file.LastWriteTimeUtc.Ticks) {
                        $complete++
                        if (-not (Test-Path -LiteralPath $readyPath)) { Set-Content -LiteralPath $readyPath -Value $entry.source_path -Encoding utf8 }
                    }
                } catch [IO.IOException] { }
            }
        }
    }
    $previousStable = $stable
    if ($transferred -ne $previousBytes -or $complete -gt $previousComplete) { $lastProgress = [DateTime]::UtcNow }
    $previousBytes = $transferred
    $previousComplete = [Math]::Max($previousComplete,$complete)
    $speed = if ($timer.Elapsed.TotalSeconds -gt 0) { $transferred / $timer.Elapsed.TotalSeconds } else { 0 }
    $eta = if ($speed -gt 0) { [Math]::Ceiling(($totalBytes-$transferred)/$speed) } else { $null }
    [ordered]@{phase='camera_transfer'; completed=$complete; total=$pending.Count; bytes=$transferred;
        total_bytes=$totalBytes; bytes_per_second=[Math]::Round($speed); eta_seconds=$eta;
        elapsed_seconds=[Math]::Round($timer.Elapsed.TotalSeconds); batch_calls=$batches} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot 'transfer-progress.json') -Encoding utf8
    if ($complete -eq $pending.Count) { break }
    if (([DateTime]::UtcNow-$lastProgress).TotalSeconds -gt $StallSeconds) { throw 'Camera transfer stalled; retain staging and inspect before retrying' }
    Start-Sleep -Seconds 2
} while ($true)
[ordered]@{completed=$complete; bytes=$totalBytes; seconds=[Math]::Round($timer.Elapsed.TotalSeconds,2); batch_calls=$batches} | ConvertTo-Json -Compress
