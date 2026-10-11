param(
  [Parameter(Mandatory = $true)][string]$ConfigPath,
  [Parameter(Mandatory = $true)][string]$StatusPath,
  [switch]$AfterExit
)

$ErrorActionPreference = 'Stop'
$resolvedConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
$resolvedStatusPath = [System.IO.Path]::GetFullPath($StatusPath)
$statusDirectory = [System.IO.Path]::GetDirectoryName($resolvedStatusPath)
$utf8Encoding = [System.Text.UTF8Encoding]::new($false, $true)
$syncValues = [ordered]@{
  write_sidecar_files = 'on import'
  run_crawler_on_start = 'TRUE'
  run_crawler_in_background = 'TRUE'
}

function Set-SyncStatus([string]$Phase, [object]$Details) {
  $report = [ordered]@{
    phase = $Phase
    updated_at_utc = [DateTime]::UtcNow.ToString('o')
    config_path = $resolvedConfigPath
    expected_values = $syncValues
    details = $Details
  }
  $reportJson = $report | ConvertTo-Json -Depth 5
  [System.IO.File]::WriteAllText($resolvedStatusPath, $reportJson, $utf8Encoding)
}

function Get-DarktableProcesses {
  @(Get-Process -Name darktable -ErrorAction SilentlyContinue)
}

try {
  if([System.IO.Path]::GetFileName($resolvedConfigPath) -ne 'darktablerc') {
    throw 'ConfigPath must name the existing darktablerc file'
  }
  if(-not [System.IO.File]::Exists($resolvedConfigPath)) {
    throw 'darktablerc does not exist at ConfigPath'
  }
  [System.IO.Directory]::CreateDirectory($statusDirectory) | Out-Null

  while($true) {
    if($AfterExit) {
      $runningInstances = @(Get-DarktableProcesses)
      while($runningInstances.Count -gt 0) {
        Set-SyncStatus 'waiting_for_darktable_exit' @{
          process_ids = @($runningInstances | ForEach-Object { $_.Id })
        }
        foreach($runningInstance in $runningInstances) {
          try {
            $runningInstance.WaitForExit()
          } catch [System.InvalidOperationException] {
            if(-not $runningInstance.HasExited) { throw }
          }
        }
        $runningInstances = @(Get-DarktableProcesses)
      }
    }

    $previousText = [System.IO.File]::ReadAllText($resolvedConfigPath, $utf8Encoding)
    $updatedText = $previousText
    $lineEnding = if($previousText.Contains("`r`n")) { "`r`n" } else { "`n" }
    foreach($settingKey in $syncValues.Keys) {
      $keyPattern = '(?m)^' + [regex]::Escape($settingKey) + '=[^\r\n]*'
      $keyMatches = [regex]::Matches($updatedText, $keyPattern)
      if($keyMatches.Count -gt 1) { throw "Duplicate setting: $settingKey" }
      $replacement = $settingKey + '=' + $syncValues[$settingKey]
      if($keyMatches.Count -eq 1) {
        $updatedText = [regex]::Replace($updatedText, $keyPattern, $replacement)
      } else {
        if($updatedText.Length -gt 0 -and -not $updatedText.EndsWith("`n")) {
          $updatedText += $lineEnding
        }
        $updatedText += $replacement + $lineEnding
      }
    }

    $temporaryPath = $resolvedConfigPath + '.sync-' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
      if($updatedText -cne $previousText) {
        [System.IO.File]::WriteAllText($temporaryPath, $updatedText, $utf8Encoding)
        if($AfterExit -and @(Get-DarktableProcesses).Count -gt 0) { continue }
        $backupName = if($AfterExit) { 'darktablerc-before-final' } else { 'darktablerc-before-file-edit' }
        $backupPath = Join-Path $statusDirectory $backupName
        [System.IO.File]::Replace($temporaryPath, $resolvedConfigPath, $backupPath)
      }
    } finally {
      if([System.IO.File]::Exists($temporaryPath)) { [System.IO.File]::Delete($temporaryPath) }
    }

    $verifiedText = [System.IO.File]::ReadAllText($resolvedConfigPath, $utf8Encoding)
    if($verifiedText -cne $updatedText) { throw 'Configuration changed before verification completed' }
    $remainingInstances = @(Get-DarktableProcesses)
    $phase = if($remainingInstances.Count -gt 0) { 'file_written_pending_exit' } else { 'applied' }
    Set-SyncStatus $phase @{
      changed = ($updatedText -cne $previousText)
      takes_effect = 'next darktable startup'
    }
    Write-Output ($syncValues | ConvertTo-Json -Compress)
    break
  }
} catch {
  if([System.IO.Directory]::Exists($statusDirectory)) {
    Set-SyncStatus 'error' @{ message = $_.Exception.Message }
  }
  Write-Error $_
  exit 1
}
