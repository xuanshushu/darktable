"""Background photo ingest with a portable NAS ledger; Python standard library only."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat as stat_module
import struct
import subprocess
import time
import uuid

EXTENSIONS = {".arw", ".jpg", ".jpeg", ".dng", ".tif", ".tiff"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def safe_relative(value):
    value = str(value).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or ":" in value or any(c in value for c in "\n\r\t"):
        raise ValueError("unsafe relative path: " + value)
    return path.as_posix()


def inside(root, relative):
    root = Path(root).resolve()
    result = root.joinpath(*PurePosixPath(safe_relative(relative)).parts).resolve()
    if not result.is_relative_to(root):
        raise ValueError("path escaped its root: " + str(root) + " -> " + str(result))
    return result


def signature(manifest, item):
    identity = [manifest["camera_id"], manifest["storage_name"], safe_relative(item["source_path"]), item["day"], int(item["size"])]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def ledger_path(destination):
    return Path(destination) / ".darktable-import" / "ledger.json"


def ledger_load(destination):
    path = ledger_path(destination)
    value = load(path) if path.exists() else {"schema_version": 1, "revision": 0, "entries": {}}
    if value.get("schema_version") != 1 or not isinstance(value.get("entries"), dict):
        raise ValueError("unsupported or invalid ledger")
    return value


@contextmanager
def ledger_lock(destination):
    directory = ledger_path(destination).parent
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "write.lock"
    token = uuid.uuid4().hex
    try:
        with lock.open("x", encoding="utf-8") as stream:
            json.dump({"token": token, "host": socket.gethostname(), "pid": os.getpid()}, stream)
    except FileExistsError:
        raise RuntimeError("another run holds the ledger lock: " + str(lock))
    try:
        yield
    finally:
        if lock.exists() and load(lock).get("token") == token:
            lock.unlink()


def ledger_merge(destination, updates):
    if not updates:
        return
    with ledger_lock(destination):
        value = ledger_load(destination)
        for key, update in updates.items():
            current = value["entries"].get(key, {})
            if current.get("target_rel") not in (None, update["target_rel"], update.get("replaces_target_rel")):
                raise RuntimeError("concurrent ledger target conflict: " + key)
            catalogs = dict(current.get("catalogs", {}))
            catalogs.update(update.get("catalogs", {}))
            value["entries"][key] = {**current, **update, "catalogs": catalogs}
        value["revision"] += 1
        value["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic_json(ledger_path(destination), value)


def existing_files(destination, directories=None, names=None):
    records = {}
    tree = None
    if directories is not None:
        tree = {}
        for relative in directories:
            branch = tree
            for part in PurePosixPath(safe_relative(relative)).parts:
                branch = branch.setdefault(part, {})
            branch[""] = True
    pending = [(destination, tree)]
    while pending:
        directory, branch = pending.pop()
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            continue
        with entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != ".darktable-import" and (branch is None or entry.name in branch):
                        # junctions must not let metadata discovery leave the archive root
                        if getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                            continue
                        pending.append((Path(entry.path), None if branch is None else branch[entry.name]))
                    continue
                if branch is not None and not branch.get(""):
                    continue
                if Path(entry.name).suffix.lower() not in EXTENSIONS:
                    continue
                if names is not None and entry.name.casefold() not in names:
                    continue
                stat = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                relative = path.relative_to(destination).as_posix()
                matches = re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", str(directory))
                records[relative] = {"name": entry.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                     "day": matches[-1] if matches else None}
    return records


def make_record(manifest, item, relative, info, verification, digest=None):
    return {"camera_id": manifest["camera_id"], "storage_name": manifest["storage_name"],
            "source_path": safe_relative(item["source_path"]), "name": item["name"], "day": item["day"],
            "size": int(item["size"]), "target_rel": safe_relative(relative), "target_mtime_ns": info["mtime_ns"],
            "sha256": digest, "verification": verification, "state": "archived", "catalogs": {}}


def plan(args):
    started = time.monotonic()
    manifest = load(args.manifest)
    destination = Path(args.dest).resolve()
    if not destination.is_dir():
        raise FileNotFoundError(destination)
    ledger = ledger_load(destination)
    directories = None
    relevant_records = [ledger["entries"][key] for key in (signature(manifest, item) for item in manifest["files"])
                        if key in ledger["entries"]]
    names = {item["name"].casefold() for item in manifest["files"]}
    names.update(PurePosixPath(record["target_rel"]).name.casefold() for record in relevant_records)
    if ledger["entries"]:
        directories = {str(PurePosixPath(item["day"][:4], item["day"])) for item in manifest["files"]}
        directories.update(str(PurePosixPath(record["target_rel"]).parent) for record in relevant_records)
    files = existing_files(destination, directories, names)
    updates = {}
    source_lookup = {(item["day"], item["name"], int(item["size"])): item for item in manifest["files"]}
    if args.seed:
        for line in Path(args.seed).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            item = source_lookup.get((row["source_day"], row["name"], int(row["size"])))
            target = Path(row["target"]).resolve()
            if not item or not target.is_relative_to(destination):
                continue
            relative = target.relative_to(destination).as_posix()
            info = files.get(relative)
            if info and info["size"] == int(item["size"]):
                key = signature(manifest, item)
                if key not in ledger["entries"]:
                    updates[key] = make_record(manifest, item, relative, info, "sha256_verified_previous_run", row["sha256"])
    ledger["entries"].update(updates)
    metadata_index = {}
    for relative, info in files.items():
        metadata_index.setdefault((info["day"], info["name"].casefold(), info["size"]), []).append(relative)
    result = []
    seen = set()
    for source in manifest["files"]:
        item = dict(source)
        safe_relative(item["source_path"])
        dt.date.fromisoformat(item["day"])
        if Path(item["name"]).name != item["name"] or int(item["size"]) <= 0:
            raise ValueError("invalid source record")
        key = signature(manifest, item)
        if key in seen:
            raise ValueError("duplicate source identity in manifest")
        seen.add(key)
        item["key"] = key
        record = ledger["entries"].get(key)
        info = files.get(record["target_rel"]) if record else None
        if record and info and info["size"] == int(item["size"]) and info["mtime_ns"] == record.get("target_mtime_ns"):
            item.update(action="cached", target_rel=record["target_rel"], verification=record["verification"])
        elif record:
            item.update(action="conflict", target_rel=record["target_rel"],
                        reason="recorded_target_changed" if info else "recorded_target_missing")
        else:
            matches = metadata_index.get((item["day"], item["name"].casefold(), int(item["size"])), [])
            if len(matches) == 1:
                relative = matches[0]
                updates[key] = make_record(manifest, item, relative, files[relative], "metadata_match")
                item.update(action="existing", target_rel=relative, verification="metadata_match")
            else:
                relative = str(PurePosixPath(item["day"][:4], item["day"], item["name"]))
                if relative in files or len(matches) > 1:
                    item.update(action="conflict", target_rel=relative, reason="ambiguous_existing_file")
                else:
                    item.update(action="copy", target_rel=relative)
        result.append(item)
    ledger_merge(destination, updates)
    counts = dict(Counter(item["action"] for item in result))
    output = {"schema_version": 1, "camera_id": manifest["camera_id"], "camera_name": manifest["camera_name"],
              "destination": str(destination), "catalog_destination": str(Path(args.dest).absolute()),
              "manifest": str(Path(args.manifest).resolve()),
              "files": result, "summary": {"camera_files": len(result), "nas_photos": len(files), "actions": counts,
                  "copy_bytes": sum(int(item["size"]) for item in result if item["action"] in ("copy", "conflict")),
                  "content_bytes_read": 0, "seconds": round(time.monotonic()-started, 3)}}
    atomic_json(Path(args.run) / "plan.json", output)
    print(json.dumps(output["summary"], ensure_ascii=False), flush=True)


def exif_time(path):
    # Sony ARW stores DateTimeOriginal in a standard TIFF EXIF directory
    with Path(path).open("rb") as stream:
        header = stream.read(8)
        base = 0
        if header[:2] == b"\xff\xd8":
            stream.seek(2)
            while True:
                if stream.read(1) != b"\xff":
                    raise ValueError("invalid JPEG metadata")
                marker = stream.read(1)
                while marker == b"\xff":
                    marker = stream.read(1)
                if marker in (b"\xda", b"\xd9", b""):
                    raise ValueError("EXIF is missing")
                length = struct.unpack(">H", stream.read(2))[0]
                payload = stream.tell()
                if marker == b"\xe1" and stream.read(6) == b"Exif\x00\x00":
                    base = payload + 6
                    header = stream.read(8)
                    break
                stream.seek(payload + length - 2)
        if header[:2] not in (b"II", b"MM"):
            raise ValueError("unsupported EXIF container")
        order = "<" if header[:2] == b"II" else ">"
        if struct.unpack(order+"H", header[2:4])[0] != 42:
            raise ValueError("unsupported TIFF version")
        limit = Path(path).stat().st_size
        pending = [struct.unpack(order+"I", header[4:8])[0]]
        visited = set()
        while pending:
            offset = pending.pop()
            if offset in visited:
                continue
            if not 8 <= base + offset < limit - 2 or len(visited) > 32:
                raise ValueError("invalid EXIF offset")
            visited.add(offset)
            stream.seek(base + offset)
            count = struct.unpack(order+"H", stream.read(2))[0]
            if count > 4096 or base+offset+2+12*count+4 > limit:
                raise ValueError("invalid EXIF directory")
            entries = stream.read(12*count)
            following = struct.unpack(order+"I", stream.read(4))[0]
            if following:
                pending.append(following)
            for pos in range(count):
                entry = entries[pos*12:(pos+1)*12]
                tag, kind, number, value = struct.unpack(order+"HHII", entry)
                if tag == 0x8769 and kind == 4 and number == 1:
                    pending.append(value)
                if tag == 0x9003 and kind == 2 and 19 <= number <= 64:
                    if base+value+number > limit:
                        raise ValueError("invalid EXIF date")
                    stream.seek(base+value)
                    text = stream.read(number).rstrip(b"\x00").decode("ascii")
                    return dt.datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
        raise ValueError("DateTimeOriginal is missing")


def digest_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b""):
            value.update(block)
    return value.hexdigest()


def transfer_ready(path, size):
    try:
        if path.stat().st_size != size:
            return False
    except FileNotFoundError:
        return False
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
                                  ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    kernel.CloseHandle(handle)
    return True


def copy_photo(request, item, run):
    destination = Path(request["destination"])
    source = inside(Path(run)/"staging", item["source_path"])
    if source.stat().st_size != int(item["size"]):
        raise IOError("incomplete camera transfer: " + item["source_path"])
    captured = exif_time(source)
    relative = str(PurePosixPath(str(captured.year), captured.strftime("%Y-%m-%d"), item["name"]))
    target = inside(destination, relative)
    digest = None
    status = "copied"
    if target.exists() or item["action"] == "conflict":
        digest = digest_file(source)
        candidate = inside(destination, item["target_rel"])
        if candidate.is_file() and candidate.stat().st_size == int(item["size"]) and digest_file(candidate) == digest:
            target = candidate
            status = "content_duplicate"
        elif target.exists():
            target = target.with_name(target.stem+"_"+digest[:12]+target.suffix)
            if target.exists():
                if digest_file(target) == digest:
                    status = "content_duplicate"
                else:
                    raise FileExistsError(target)
    if status == "copied":
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name("."+target.name+"."+uuid.uuid4().hex+".part")
        hasher = hashlib.sha256()
        count = 0
        with source.open("rb") as incoming, partial.open("xb") as outgoing:
            for block in iter(lambda: incoming.read(4*1024*1024), b""):
                count += len(block)
                hasher.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        streamed_digest = hasher.hexdigest()
        if count != int(item["size"]) or partial.stat().st_size != count or (digest and digest != streamed_digest):
            raise IOError("transfer validation failed: " + str(partial))
        digest = streamed_digest
        if os.name != "nt":
            # link publishes without replacing an existing name on POSIX; unsupported NAS links fail safely
            os.link(partial, target)
            partial.unlink()
        else:
            os.rename(partial, target)
    stat = target.stat()
    record = make_record(load(request["manifest"]), item, target.relative_to(destination).as_posix(),
                         {"mtime_ns": stat.st_mtime_ns}, "stream_sha256_and_size" if status == "copied" else "content_sha256", digest)
    record["capture_time"] = captured.isoformat()
    if item["action"] == "conflict":
        record["replaces_target_rel"] = item["target_rel"]
    record["staging_mtime_ns"] = source.stat().st_mtime_ns
    return {"key": item["key"], "status": status, "bytes": int(item["size"]), "record": record}


def archive(args):
    request = load(args.plan)
    run = Path(args.run).resolve()
    pending = {item["key"]: item for item in request["files"] if item["action"] in ("copy", "conflict")}
    updates = {}
    journal = run / "archive-results.jsonl"
    if journal.exists():
        previous = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        directories = {str(PurePosixPath(row["record"]["target_rel"]).parent) for row in previous}
        names = {PurePosixPath(row["record"]["target_rel"]).name.casefold() for row in previous}
        targets = existing_files(Path(request["destination"]).resolve(), directories, names)
        for row in previous:
            if row["key"] in pending:
                target = targets.get(row["record"]["target_rel"])
                if target and target["size"] == row["record"]["size"] and target["mtime_ns"] == row["record"].get("target_mtime_ns"):
                    updates[row["key"]] = row["record"]
                    del pending[row["key"]]
    started = time.monotonic()
    futures = {}
    completed = len(updates)
    total = completed + len(pending)
    copied_bytes = sum(record["size"] for record in updates.values())
    initial_bytes = copied_bytes
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        while pending or futures:
            if getattr(args, "stop_event", None) is not None and args.stop_event.is_set():
                raise RuntimeError("archive stopped; retain staging and the run journal")
            if time.monotonic()-started > args.timeout:
                raise TimeoutError("archive timed out; keep staging and the run journal")
            for key, item in list(pending.items()):
                if len(futures) >= args.workers:
                    break
                ready = run / "ready" / (key+".ready")
                if args.wait and not ready.exists():
                    source = inside(run/"staging", item["source_path"])
                    if not transfer_ready(source, int(item["size"])):
                        continue
                futures[executor.submit(copy_photo, request, item, run)] = key
                del pending[key]
            done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED) if futures else (set(), None)
            if not futures:
                time.sleep(1)
            for future in done:
                try:
                    row = future.result()
                except Exception as error:
                    key = futures[future]
                    item = next(item for item in request["files"] if item["key"] == key)
                    raise RuntimeError("archive failed for " + item["source_path"] + ": " + str(error)) from error
                del futures[future]
                updates[row["key"]] = row["record"]
                with journal.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False)+"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                completed += 1
                copied_bytes += row["bytes"]
                if completed % 20 == 0:
                    ledger_merge(request["destination"], updates)
                print(json.dumps({"completed": completed, "total": total, "last": row["record"]["name"]}), flush=True)
            elapsed = time.monotonic()-started
            atomic_json(run/"archive-progress.json", {"phase": "nas_archive", "completed": completed, "total": total,
                "bytes": copied_bytes, "elapsed_seconds": round(elapsed), "bytes_per_second": round((copied_bytes-initial_bytes)/max(elapsed,1))})
    ledger_merge(request["destination"], updates)
    summary = {"processed": total, "copied_bytes": copied_bytes, "seconds": round(time.monotonic()-started,2)}
    atomic_json(run/"archive-summary.json", summary)
    print(json.dumps(summary), flush=True)


def lua_string(value):
    for length in range(1, 12):
        middle = "="*length
        if "]"+middle+"]" not in value:
            return "["+middle+"["+value+"]"+middle+"]"
    raise ValueError("unsupported Lua string")


def cleanup(args):
    request = load(args.plan)
    run = Path(args.run).resolve()
    ledger = ledger_load(request["destination"])
    candidates = [item for item in request["files"] if item["action"] in ("copy", "conflict")]
    records = [ledger["entries"].get(item["key"]) for item in candidates]
    if any(not record or record["state"] != "cataloged" or args.catalog_id not in record["catalogs"] for record in records):
        raise RuntimeError("cleanup requires successful archive and catalog records for every staged photo")
    destination = Path(request["destination"]).resolve()
    directories = {str(PurePosixPath(record["target_rel"]).parent) for record in records}
    names = {PurePosixPath(record["target_rel"]).name.casefold() for record in records}
    targets = existing_files(destination, directories, names)
    removable = []
    for item, record in zip(candidates, records):
        target = targets.get(record["target_rel"])
        if not target or target["size"] != record["size"] or target["mtime_ns"] != record["target_mtime_ns"]:
            raise RuntimeError("NAS target changed; retain staging: " + record["target_rel"])
        source = inside(run/"staging", item["source_path"])
        if not source.exists():
            continue
        stat = source.stat()
        if stat.st_size != record["size"] or stat.st_mtime_ns != record.get("staging_mtime_ns"):
            raise RuntimeError("local staging changed; retain it: " + str(source))
        removable.append(source)
    removed_bytes = 0
    for source in removable:
        removed_bytes += source.stat().st_size
        if os.name == "nt" and not source.stat().st_mode & stat_module.S_IWRITE:
            # MTP preserves read-only flags on these disposable, verified staging copies
            os.chmod(source, source.stat().st_mode | stat_module.S_IWRITE)
        source.unlink()
    for directory in {source.parent for source in removable}:
        try:
            directory.rmdir()
        except OSError:
            pass
    summary = {"removed": len(removable), "bytes": removed_bytes, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    atomic_json(run/"cleanup-summary.json", summary)
    print(json.dumps(summary), flush=True)


def import_catalog(args):
    request = load(args.plan)
    run = Path(args.run).resolve()
    owner_result = subprocess.run([args.gdbus, "call", "--session", "--dest", "org.freedesktop.DBus",
        "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.GetNameOwner",
        "--timeout", "5", "org.darktable.service"], capture_output=True, text=True, timeout=10)
    owner = re.search(r":\d+\.\d+", owner_result.stdout) if owner_result.returncode == 0 else None
    if not owner:
        raise RuntimeError("no running darktable D-Bus service; archive is complete, catalog registration is pending")
    endpoint = [args.gdbus, "call", "--session", "--dest", owner.group(), "--object-path", "/darktable",
                "--method", "org.darktable.service.Remote.Lua"]
    ping = subprocess.run(endpoint+["--timeout", "15", "return tostring(#require('darktable').database)"],
                          capture_output=True, text=True, timeout=20)
    if ping.returncode:
        raise RuntimeError("darktable Lua did not respond; retain the archive and retry after app startup")
    ledger = ledger_load(request["destination"])
    catalog_root = Path(args.catalog_root or request.get("catalog_destination", request["destination"])).absolute()
    if catalog_root.resolve() != Path(request["destination"]).resolve():
        raise ValueError("catalog root must refer to the same NAS destination")
    records = [ledger["entries"].get(item["key"]) for item in request["files"]]
    if any(not record for record in records):
        raise RuntimeError("some photos have not been archived")
    directories = {str(PurePosixPath(record["target_rel"]).parent) for record in records}
    names = {PurePosixPath(record["target_rel"]).name.casefold() for record in records}
    targets = existing_files(Path(request["destination"]).resolve(), directories, names)
    rows = []
    for item, record in zip(request["files"], records):
        target = targets.get(record["target_rel"])
        if not target or target["size"] != record["size"] or target["mtime_ns"] != record["target_mtime_ns"]:
            raise RuntimeError("target missing or changed: " + record["target_rel"])
        catalog_target = catalog_root.joinpath(*PurePosixPath(safe_relative(record["target_rel"])).parts)
        rows.append((item["key"], str(catalog_target).replace("\\", "/")))
    report = run / "catalog-results.tsv"
    stats = run / "catalog-summary.json"
    table = ",\n".join("{"+lua_string(key)+","+lua_string(path)+"}" for key,path in rows)
    aliases = ",".join(lua_string(str(root).replace("\\", "/").rstrip("/").lower()+"/")
                       for root in {catalog_root, Path(request["destination"])})
    code = """local dt=require('darktable')
local before=#dt.database
local known={}
local aliases={ALIASES}
local function normalized(path)
  local value=path:gsub('\\\\','/'):lower()
  for _,root in ipairs(aliases) do
    if value:sub(1,#root)==root then return '@archive/'..value:sub(#root+1) end
  end
  return value
end
for _,img in ipairs(dt.database) do known[normalized(img.path..'/'..img.filename)]=img end
local wanted={TABLE}
local f=assert(io.open(REPORT,'w'))
local added,existing,failed=0,0,0
for _,row in ipairs(wanted) do
  local image=known[normalized(row[2])]
  local status='existing'
  if image then existing=existing+1 else
    local ok,value=pcall(dt.database.import,row[2])
    if ok and value then image=value; status='imported'; added=added+1; known[normalized(row[2])]=image
    else status='failed'; failed=failed+1 end
  end
  local date=image and image.exif_datetime_taken or ''
  f:write(row[1],'\\t',status,'\\t',tostring(image and image.id or 0),'\\t',row[2],'\\t',date,'\\n'); f:flush()
end
f:close()
local summary=string.format('{"before":%d,"after":%d,"imported":%d,"existing":%d,"failed":%d}',before,#dt.database,added,existing,failed)
local s=assert(io.open(STATS,'w')); s:write(summary); s:close()
return summary
""".replace("ALIASES",aliases).replace("TABLE",table).replace("REPORT",lua_string(str(report).replace("\\","/"))).replace("STATS",lua_string(str(stats).replace("\\","/")))
    script = run / "import.lua"
    script.write_text(code, encoding="utf-8")
    expression = "return dofile("+lua_string(str(script).replace("\\","/"))+")"
    command = endpoint+["--timeout", "1800", expression]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=1830)
    if result.returncode:
        raise RuntimeError("background import failed: "+result.stderr.strip())
    catalog_updates = {}
    for line in report.read_text(encoding="utf-8").splitlines():
        key,status,imgid,target,captured = line.split("\t")
        if status == "failed":
            continue
        record = dict(ledger["entries"][key])
        record["state"] = "cataloged"
        record["catalogs"] = {args.catalog_id: {"imgid": int(imgid), "target": target, "capture_time": captured}}
        catalog_updates[key] = record
    ledger_merge(request["destination"], catalog_updates)
    summary = load(stats)
    print(json.dumps(summary), flush=True)
    if summary["failed"]:
        raise RuntimeError("some photos failed to enter the catalog; inspect catalog-results.tsv")


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("plan")
    command.add_argument("--manifest", required=True)
    command.add_argument("--dest", required=True)
    command.add_argument("--run", required=True)
    command.add_argument("--seed")
    command = commands.add_parser("archive")
    command.add_argument("--plan", required=True)
    command.add_argument("--run", required=True)
    command.add_argument("--workers", type=int, choices=range(1,5), default=2)
    command.add_argument("--wait", action="store_true")
    command.add_argument("--timeout", type=int, default=7200)
    command = commands.add_parser("import")
    command.add_argument("--plan", required=True)
    command.add_argument("--run", required=True)
    command.add_argument("--gdbus", required=True)
    command.add_argument("--catalog-id", required=True)
    command.add_argument("--catalog-root")
    command = commands.add_parser("cleanup")
    command.add_argument("--plan", required=True)
    command.add_argument("--run", required=True)
    command.add_argument("--catalog-id", required=True)
    args = parser.parse_args()
    state_path = Path(args.run)/"phase-state.json"
    state = {"phase": args.command, "status": "running", "at": dt.datetime.now(dt.timezone.utc).isoformat()}
    atomic_json(state_path, state)
    try:
        {"plan": plan, "archive": archive, "import": import_catalog, "cleanup": cleanup}[args.command](args)
    except Exception as error:
        atomic_json(state_path, {**state, "status": "failed", "error": str(error)})
        raise
    atomic_json(state_path, {**state, "status": "complete"})


if __name__ == "__main__":
    main()
