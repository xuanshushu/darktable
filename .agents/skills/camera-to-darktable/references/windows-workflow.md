# Windows MTP workflow

## Set up another computer

Clone or pull this repository and open it as a Codex project. This skill lives at `.agents/skills/camera-to-darktable`, a supported repository discovery location ([official documentation](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)). Use `$camera-to-darktable` or ask to import camera photos. If it is not visible yet, restart Codex. A global skill with the same name may also appear; choose this repository copy when working in this project.

Requirements: Windows, PowerShell 7 (`pwsh`), Python 3.10 or newer, and a running darktable with Lua enabled and a working session D-Bus client (`gdbus.exe`). No extra Python packages are required. Discover the current interpreter and actual darktable installation; never reuse another computer's absolute paths. The camera adapter expects one Sony storage with `YYYY-MM-DD` folders.

Copy `settings.example.json` to `settings.local.json` in the skill directory. Set `destination` to the intended existing NAS photo root, `run_root` to local storage outside the repository with enough space, and `gdbus` to the actual client executable. Check `camera_name`, cache/batch sizes, workers and the progress port. The example expands `%TEMP%` for local storage and `%COMPUTERNAME%` for the catalog ID. Separate libraries on one computer need different catalog IDs; do not copy another computer's ID.

`settings.local.json` is ignored by Git. Use the computer's existing authenticated NAS connection; do not put credentials, photos, ledgers, run logs, database copies or bytecode into version control. The shared NAS ledger stores relative target paths, so computers can use different mount paths while registering each library separately.

macOS can load this skill and read its documentation. `camera.ps1` uses Windows MTP APIs; no complete macOS ingest workflow or tested macOS camera adapter exists. Do not claim macOS camera support or run the Windows adapter there.

## Run commands

Use a fresh run directory. Plan completes before transfer starts. The rolling helper runs camera transfer and NAS archival concurrently, reserves expected batch sizes against the cache limit, and imports/cleans each batch before releasing that reservation. Default batches are at most 512 MiB and total staged/in-flight photos at most 2 GiB. A photo above the batch size runs alone; a photo above the cache limit fails before copying. Cache parameters use MiB (2048 MiB = 2 GiB).

```powershell
$skillRoot = (Resolve-Path '.agents/skills/camera-to-darktable').Path
$configPath = Join-Path $skillRoot 'settings.local.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $skillRoot 'settings.example.json') -Destination $configPath
    throw 'Fill in settings.local.json before continuing'
}
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$destination = [Environment]::ExpandEnvironmentVariables($settings.destination)
$scratchRoot = [Environment]::ExpandEnvironmentVariables($settings.run_root)
$gdbusExe = [Environment]::ExpandEnvironmentVariables($settings.gdbus)
$catalogId = [Environment]::ExpandEnvironmentVariables($settings.catalog_id)
if (-not $destination -or -not (Test-Path -LiteralPath $destination -PathType Container)) {
    throw 'Set destination to the intended existing NAS photo root'
}
if (-not $scratchRoot -or -not $catalogId -or -not (Test-Path -LiteralPath $gdbusExe -PathType Leaf)) {
    throw 'Check run_root, catalog_id and gdbus in settings.local.json'
}
$pythonExe = (Get-Command python -ErrorAction Stop).Source
& $pythonExe -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10 or newer is required"'
if ($LASTEXITCODE -ne 0) { throw 'Select a working Python 3.10+ interpreter' }
$runRoot = Join-Path $scratchRoot ('sony-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
pwsh -NoProfile -File "$skillRoot\scripts\camera.ps1" -Action Scan -CameraName $settings.camera_name -Output "$runRoot\source.json"
if ($LASTEXITCODE -ne 0) { throw 'Camera scan failed' }
& $pythonExe -X utf8 "$skillRoot\scripts\ingest.py" plan --manifest "$runRoot\source.json" --dest $destination --run $runRoot
if ($LASTEXITCODE -ne 0) { throw 'Archive plan failed' }

# start the dashboard in a separate execution session after reviewing plan.json
& $pythonExe -X utf8 "$skillRoot\scripts\progress.py" --run $runRoot --port $settings.progress_port

# after confirming that the running app exposes LuaEnabled
& $pythonExe -X utf8 "$skillRoot\scripts\rolling.py" --plan "$runRoot\plan.json" --run $runRoot --gdbus $gdbusExe --catalog-id $catalogId --catalog-root $destination --cache-mib $settings.cache_mib --batch-mib $settings.batch_mib --workers $settings.workers
```

Resolve the skill path from the loaded SKILL.md; the example assumes the shell is at the repository root. Select a verified interpreter (`python3` or a resolved `py -3` executable can be used instead). Supply these variables again in separate shell sessions.

Open `http://127.0.0.1:<configured-port>/` with `open_in_codex` or provide the actual clickable URL. The read-only server binds only loopback and serves progress, never photos. If the port is occupied, choose another free local port.

Check background Lua availability without changing the active image:

```powershell
& $gdbusExe call --session --dest org.darktable.service --object-path /darktable --method org.freedesktop.DBus.Properties.Get org.darktable.service.Remote LuaEnabled
```

If Lua is unavailable, report that rolling import needs the current app to respond before its cache can be released. If it becomes unavailable mid-run, the helper stops subsequent batches and retains the bounded staging. Do not replace the open app's library, force-close it, or use screen automation without the user requesting it. Finishing a whole card as archive-only work requires a separately chosen storage/release policy; do not silently switch to unbounded staging.

The import helper checks the existing D-Bus owner and a short Lua ping first, then calls that unique owner so an app restart cannot silently redirect the batch or auto-start a different instance. If the user restarts darktable during a check, retain the successful import records and retry only when the new instance responds. A saved catalog can be inspected read-only to verify completed results; do not write it directly.

Use the catalog's existing mount spelling for imported paths. `--catalog-root` must resolve to the same destination. The live lookup treats that mount and its resolved archive root as aliases so an existing photo is not reimported under a second spelling. Do not infer a mapped drive's provider merely from installed RaiDrive software; verify the actual mount. A second share spelling of the same NAS folder still needs alias handling before switching catalog paths.

## Record and recovery

`ledger.json` stays on the NAS, under `.darktable-import`. It contains source identity, target relative path and mtime, verification mode, SHA-256 when calculated, archival state and a map of catalog IDs. The source identity hash hashes a short metadata tuple; it is not a photo content hash.

The local run contains `source.json`, `plan.json`, `rolling-layout.json`, `rolling-progress.json`, `rolling-summary.json` and a `batches/0000...` directory. Each batch contains its own plan, staging, ready markers, archive journal, camera output log, import Lua/TSV, summaries and successful completion marker. Small-batch completion merges into the NAS ledger before registration/cleanup. Local journal entries preserve completed copies if a ledger checkpoint was interrupted. Retain audit logs; the helper automatically releases only verified photo staging.

Resume by rerunning the same `rolling.py` command, including its cache and catalog parameters. Retained batches are processed first. Journaled photos with unchanged NAS size/mtime skip camera reads even if catalog registration is pending. Verified, cleaned, cataloged batches keep their reports and are skipped. The camera adapter also skips already complete local copies; partial/busy staging stops safely. A lock prevents two rolling processes from using the same run. If a process crashed and left its lock, inspect the recorded PID before any manual recovery; never automatically steal it.

Unexpected staging files, changed plans/parameters or retained staging larger than the budget stop the helper before new copying. Camera copy errors/timeouts retain this batch; Windows may finish an already submitted Shell copy after its client stops, so wait for its writer to close before resuming. The cache reservation still bounds those submitted files. Inspect partials individually; do not delete camera originals or recursively clean the NAS.

For a partial date folder, the Sony adapter filters a `FolderItems` collection by each item's current parsing-path leaf (the device's object ID). Filtering by the displayed filename returned an empty collection on this device. The selected names/count must exactly match the planned subset before `CopyHere`; a broad filter must never bypass the reserved byte budget. The IDs are resolved anew, not saved as long-term dedup identity. See Microsoft's [FolderItems3.Filter](https://learn.microsoft.com/en-us/windows/win32/shell/folderitems3-filter) and [Folder.CopyHere](https://learn.microsoft.com/en-us/windows/win32/shell/folder-copyhere) API references; provider behavior still needs actual device testing.

Repeat a new scan and plan after completion. A normal unchanged card yields `cached` for all previous successful entries and `copy_bytes: 0`, `content_bytes_read: 0`. Changes to a recorded target route to conflict handling. A new camera mount identity, reformatted card or renumbering needs review instead of assuming identity continuity.

The initial ledger bootstrap scans existing photo metadata. Later plans inspect date directories relevant to the card and prior recorded targets. A slow NAS can still make metadata operations noticeable, but repeat runs do not read RAW contents.

Rolling batches are cleaned automatically only after their camera process has exited, NAS archival and catalog registration have succeeded, and guarded metadata checks pass. The following command remains useful for an older bulk run or a single retained batch, using that batch's plan/root. It checks the whole candidate set before deleting any local photo and retains run logs. It does not delete source or NAS photos.

```powershell
& $pythonExe -X utf8 "$skillRoot\scripts\ingest.py" cleanup --plan "$runRoot\plan.json" --run $runRoot --catalog-id $catalogId
```

For changes, run `python -X utf8 scripts/test_ingest.py` and `python -X utf8 scripts/test_rolling.py`. The latter uses actual local archival with simulated camera/catalog adapters to check cache limits, concurrent stages, failure retention, retry without camera rereads and completed-batch recovery. Keep an actual MTP/darktable check distinct from those simulations.

Windows Sony metadata scanning and earlier bulk MTP transfer were tested. An unchanged-card check copied zero bytes and added zero catalog entries. Rolling NAS hashes, live darktable and cleanup were tested with existing NAS photos under a smaller cache limit, with no new catalog entries and staging released. That fixture substituted NAS input for the camera: current camera downloads did not return, including a direct invocation outside the helper. Rolling MTP downloads remain unverified. JPEG/TIFF/DNG and macOS camera integration also lack actual device validation.
