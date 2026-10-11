"""Bounded camera staging with overlapping NAS archival and guarded batch cleanup."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

sys.dont_write_bytecode = True
import ingest

MIB = 1024 * 1024


def split_batches(request, batch_bytes, cache_bytes):
    if not 0 < batch_bytes <= cache_bytes:
        raise ValueError("batch size must be positive and no larger than the cache limit")
    cached = []
    batches = []
    current = []
    current_bytes = 0
    paths = set()
    keys = set()
    for item in request["files"]:
        relative = ingest.safe_relative(item["source_path"])
        if not re.fullmatch(r"[0-9a-f]{64}", item["key"]):
            raise ValueError("invalid source identity key")
        if relative.casefold() in paths or item["key"] in keys:
            raise ValueError("duplicate source path or identity in plan")
        paths.add(relative.casefold())
        keys.add(item["key"])
        if int(item["size"]) <= 0:
            raise ValueError("invalid photo size")
        if item["action"] not in {"copy", "conflict"}:
            cached.append(item)
            continue
        size = int(item["size"])
        if size > cache_bytes:
            raise ValueError("a photo exceeds the cache limit; increase --cache-mib: " + relative)
        if current and current_bytes + size > batch_bytes:
            batches.append(current)
            current, current_bytes = [], 0
        current.append(item)
        current_bytes += size
    if current:
        batches.append(current)
    return ([cached] if cached else []) + batches


@contextmanager
def run_lock(run):
    path = run / "rolling.lock"
    token = uuid.uuid4().hex
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "token": token}, stream)
    except FileExistsError:
        raise RuntimeError("another rolling run holds the lock: " + str(path))
    try:
        yield
    finally:
        if path.exists() and ingest.load(path).get("token") == token:
            path.unlink()


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.request = ingest.load(args.plan)
        self.run = Path(args.run).resolve()
        self.run.mkdir(parents=True, exist_ok=True)
        self.cache_bytes = int(args.cache_mib * MIB)
        self.batch_bytes = int(args.batch_mib * MIB)
        groups = split_batches(self.request, self.batch_bytes, self.cache_bytes)
        self.batches = []
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.report_stop = threading.Event()
        self.jobs = queue.Queue()
        self.reservations = {}
        self.peak_reserved = 0
        self.peak_cache = 0
        self.error = None
        self.complete = False
        self.started = time.monotonic()
        self.layout = {"schema_version": 1, "cache_bytes": self.cache_bytes, "batch_bytes": self.batch_bytes,
                       "catalog_id": args.catalog_id, "catalog_root": args.catalog_root,
                       "plan_sha256": hashlib.sha256(json.dumps(self.request, sort_keys=True).encode("utf-8")).hexdigest()}
        layout_path = self.run / "rolling-layout.json"
        if layout_path.exists() and ingest.load(layout_path) != self.layout:
            raise ValueError("resume requires the same plan, catalog and cache settings; use a fresh run for a new plan")
        # an earlier bulk run may have more local photos than this bounded pipeline can account for
        if (self.run / "staging").exists() and any((self.run / "staging").rglob("*.*")):
            raise ValueError("bulk staging exists; finish that run or choose a fresh rolling run")
        for index, files in enumerate(groups):
            root = ingest.inside(self.run, "batches/%04d" % index)
            root.mkdir(parents=True, exist_ok=True)
            request = {**self.request, "files": files}
            subplan = root / "plan.json"
            if subplan.exists() and ingest.load(subplan) != request:
                raise ValueError("batch plan changed: " + str(subplan))
            ingest.atomic_json(subplan, request)
            weight = sum(int(x["size"]) for x in files if x["action"] in {"copy", "conflict"})
            batch = SimpleNamespace(index=index, root=root, request=request, weight=weight,
                                    event=threading.Event(), transfer_error=None, state="waiting")
            self.batches.append(batch)
            if self.cache_files(batch):
                self.reservations[index] = weight
        if sum(self.reservations.values()) > self.cache_bytes:
            raise ValueError("retained staging exceeds the cache budget; retain it and inspect before resuming")
        previous = read_json(self.run / "rolling-progress.json", {})
        self.peak_reserved = max(sum(self.reservations.values()), previous.get("peak_reserved_bytes", 0))
        self.peak_cache = previous.get("peak_cache_bytes", 0)
        ingest.atomic_json(layout_path, self.layout)

    def cache_files(self, batch):
        stage = batch.root / "staging"
        expected = {ingest.safe_relative(x["source_path"]).casefold(): int(x["size"])
                    for x in batch.request["files"] if x["action"] in {"copy", "conflict"}}
        result = []
        if stage.exists():
            if stage.is_symlink() or getattr(stage.stat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("staging root is a linked directory: " + str(stage))
            for path in stage.rglob("*"):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ValueError("staging contains a linked directory: " + str(path))
                    continue
                relative = path.relative_to(stage).as_posix()
                if stat.S_ISLNK(info.st_mode) or relative.casefold() not in expected:
                    raise ValueError("unexpected staging file; retain it: " + str(path))
                ingest.inside(stage, relative)
                size = info.st_size
                if size > expected[relative.casefold()]:
                    raise ValueError("staging file exceeds its planned size: " + str(path))
                result.append((path, size))
        return result

    def recover(self, batch):
        ledger = ingest.ledger_load(self.request["destination"])["entries"]
        journal = batch.root / "archive-results.jsonl"
        rows = {}
        if journal.exists():
            for line in journal.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                rows[row["key"]] = row
        records = {}
        for item in batch.request["files"]:
            row_record = rows.get(item["key"], {}).get("record")
            record = ledger.get(item["key"])
            if item["action"] in {"copy", "conflict"}:
                # a fresh conflict plan still requires content comparison; only this run's journal can skip it
                if not row_record:
                    continue
                if not record or any(record.get(k) != row_record.get(k) for k in ("target_rel", "target_mtime_ns", "sha256")):
                    record = row_record
            if record and record["size"] == int(item["size"]):
                records[item["key"]] = record
        directories = {str(PurePosixPath(x["target_rel"]).parent) for x in records.values()}
        names = {PurePosixPath(x["target_rel"]).name.casefold() for x in records.values()}
        targets = ingest.existing_files(Path(self.request["destination"]).resolve(), directories, names)
        valid = {}
        for key, record in records.items():
            target = targets.get(record["target_rel"])
            if target and target["size"] == record["size"] and target["mtime_ns"] == record["target_mtime_ns"]:
                valid[key] = record
        pending = [x for x in batch.request["files"] if x["action"] in {"copy", "conflict"}]
        with journal.open("a", encoding="utf-8") as stream:
            for item in pending:
                record = valid.get(item["key"])
                if record and rows.get(item["key"], {}).get("record") != record:
                    stream.write(json.dumps({"key": item["key"], "status": "resumed_archive",
                                             "bytes": record["size"], "record": record}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        finished = ((batch.root / "rolling-complete.json").exists() and not self.cache_files(batch)
                    and len(valid) == len(batch.request["files"])
                    and all(x["state"] == "cataloged" and self.args.catalog_id in x.get("catalogs", {}) for x in valid.values()))
        files = [x for x in pending if x["key"] not in valid]
        for item in files:
            source = ingest.inside(batch.root / "staging", item["source_path"])
            if not source.exists():
                ready = batch.root / "ready" / (item["key"] + ".ready")
                if ready.exists():
                    ready.unlink()
        return files, finished

    def reserve(self, batch, needed):
        with self.condition:
            while not self.stop.is_set():
                if not needed or batch.index in self.reservations:
                    return
                if sum(self.reservations.values()) + batch.weight <= self.cache_bytes:
                    self.reservations[batch.index] = batch.weight
                    self.peak_reserved = max(self.peak_reserved, sum(self.reservations.values()))
                    return
                self.condition.wait(timeout=1)
            raise RuntimeError("rolling ingest stopped; retain staging and logs")

    def transfer_batch(self, batch, files):
        if not files:
            return
        request = {**batch.request, "files": files}
        plan = batch.root / "transfer-plan.json"
        ingest.atomic_json(plan, request)
        command = [self.args.pwsh, "-NoProfile", "-File", str(Path(__file__).with_name("camera.ps1")),
                   "-Action", "Transfer", "-CameraName", self.request["camera_name"],
                   "-Plan", str(plan), "-Run", str(batch.root)]
        started = time.monotonic()
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        with (batch.root / "camera-output.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       startupinfo=startup)
            try:
                while process.poll() is None:
                    if self.stop.is_set() or time.monotonic() - started > self.args.camera_timeout:
                        raise RuntimeError("camera batch stopped or timed out; retain staging and inspect camera-output.log")
                    time.sleep(0.5)
                if process.returncode:
                    raise RuntimeError("camera batch failed; inspect " + str(batch.root / "camera-output.log"))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

    def consume(self):
        try:
            while True:
                batch = self.jobs.get()
                if batch is None:
                    return
                batch.state = "archive"
                args = SimpleNamespace(plan=str(batch.root / "plan.json"), run=str(batch.root),
                    workers=self.args.workers, wait=True, timeout=self.args.timeout, stop_event=self.stop,
                    gdbus=self.args.gdbus, catalog_id=self.args.catalog_id, catalog_root=self.args.catalog_root)
                ingest.archive(args)
                while not batch.event.wait(timeout=0.5):
                    if self.stop.is_set():
                        raise RuntimeError("camera did not finish; retain this batch")
                if batch.transfer_error:
                    raise batch.transfer_error
                batch.state = "import"
                ingest.import_catalog(args)
                batch.state = "cleanup"
                ingest.cleanup(args)
                if self.cache_files(batch):
                    raise RuntimeError("batch cleanup left local photos; retain them before continuing")
                ingest.atomic_json(batch.root / "rolling-complete.json", {"complete": True})
                batch.state = "complete"
                with self.condition:
                    self.reservations.pop(batch.index, None)
                    self.condition.notify_all()
        except Exception as error:
            batch.state = "failed"
            self.error = error
            self.stop.set()
            with self.condition:
                self.condition.notify_all()

    def snapshot(self):
        staged = staged_bytes = archived = archive_bytes = 0
        cache_bytes = 0
        catalog = {"imported": 0, "existing": 0, "failed": 0}
        summaries = []
        for batch in self.batches:
            cache_bytes += sum(size for _, size in self.cache_files(batch))
            pending = [x for x in batch.request["files"] if x["action"] in {"copy", "conflict"}]
            archive = read_json(batch.root / "archive-progress.json", {})
            archived += archive.get("completed", 0)
            archive_bytes += archive.get("bytes", 0)
            known = set()
            journal = batch.root / "archive-results.jsonl"
            if journal.exists():
                for line in journal.read_text(encoding="utf-8").splitlines():
                    try:
                        known.add(json.loads(line)["key"])
                    except ValueError:
                        pass
            for item in pending:
                if item["key"] in known or (batch.root / "ready" / (item["key"] + ".ready")).exists():
                    staged += 1
                    staged_bytes += int(item["size"])
            summary = read_json(batch.root / "catalog-summary.json", None)
            if summary:
                summaries.append(((batch.root / "catalog-summary.json").stat().st_mtime_ns, summary))
                for key in catalog:
                    catalog[key] += summary.get(key, 0)
            else:
                report = batch.root / "catalog-results.tsv"
                if report.exists():
                    for line in report.read_text(encoding="utf-8").splitlines():
                        fields = line.split("\t")
                        if len(fields) == 5 and fields[1] in catalog:
                            catalog[fields[1]] += 1
        self.peak_cache = max(self.peak_cache, cache_bytes)
        with self.condition:
            reserved = sum(self.reservations.values())
        if cache_bytes > self.cache_bytes:
            raise RuntimeError("actual staging exceeded the cache limit; retain it and inspect")
        if summaries:
            summaries.sort(key=lambda pair: pair[0])
            catalog.update(before=summaries[0][1]["before"], after=summaries[-1][1]["after"])
        return {"staged": staged, "staged_bytes": staged_bytes, "archived": archived,
                "archive_bytes": archive_bytes, "cache_bytes": cache_bytes, "reserved_bytes": reserved,
                "cache_limit_bytes": self.cache_bytes, "peak_cache_bytes": self.peak_cache,
                "peak_reserved_bytes": self.peak_reserved, "batch_bytes": self.batch_bytes,
                "catalog": catalog, "catalog_finished": self.complete,
                "elapsed_seconds": round(time.monotonic() - self.started, 2),
                "batches": [{"index": b.index, "state": b.state} for b in self.batches]}

    def report(self):
        try:
            while not self.report_stop.is_set():
                ingest.atomic_json(self.run / "rolling-progress.json", self.snapshot())
                self.report_stop.wait(timeout=1)
        except Exception as error:
            self.error = error
            self.stop.set()
            with self.condition:
                self.condition.notify_all()

    def execute(self):
        consumer = threading.Thread(target=self.consume, name="nas-catalog-consumer")
        reporter = threading.Thread(target=self.report, name="rolling-progress")
        consumer.start()
        reporter.start()
        try:
            # retained batches get priority so a resumed run can free its already reserved cache
            ordered = sorted(self.batches, key=lambda b: (b.index not in self.reservations, b.index))
            for batch in ordered:
                if self.stop.is_set():
                    raise self.error or RuntimeError("rolling ingest stopped")
                files, finished = self.recover(batch)
                if finished:
                    batch.state = "complete"
                    continue
                self.reserve(batch, bool(files) or bool(self.cache_files(batch)))
                if files and shutil.disk_usage(self.run).free < sum(int(x["size"]) for x in files) + 64*MIB:
                    raise RuntimeError("insufficient local space for the next camera batch")
                batch.state = "transfer" if files else "queued"
                self.jobs.put(batch)
                try:
                    self.transfer_batch(batch, files)
                except Exception as error:
                    batch.transfer_error = error
                    self.stop.set()
                    raise
                finally:
                    batch.event.set()
            self.jobs.put(None)
            consumer.join()
            if self.error:
                raise self.error
            self.complete = True
        except BaseException:
            self.stop.set()
            self.jobs.put(None)
            consumer.join()
            raise
        finally:
            self.report_stop.set()
            reporter.join()
            ingest.atomic_json(self.run / "rolling-progress.json", self.snapshot())
        summary = self.snapshot()
        ingest.atomic_json(self.run / "rolling-summary.json", summary)
        ingest.atomic_json(self.run / "catalog-summary.json", summary["catalog"])
        print(json.dumps(summary, ensure_ascii=False), flush=True)


def read_json(path, default):
    try:
        return ingest.load(path)
    except (OSError, ValueError):
        return default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--gdbus", required=True)
    parser.add_argument("--catalog-id", required=True)
    parser.add_argument("--catalog-root")
    parser.add_argument("--cache-mib", type=int, default=2048)
    parser.add_argument("--batch-mib", type=int, default=512)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--pwsh", default="pwsh")
    parser.add_argument("--camera-timeout", type=int, default=900)
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    run = Path(args.run).resolve()
    run.mkdir(parents=True, exist_ok=True)
    with run_lock(run):
        state = {"phase": "rolling", "status": "running", "at": time.time()}
        previous_state = read_json(run / "phase-state.json", {})
        if previous_state.get("test_source"):
            state["test_source"] = previous_state["test_source"]
        ingest.atomic_json(run / "phase-state.json", state)
        try:
            Pipeline(args).execute()
        except Exception as error:
            ingest.atomic_json(run / "phase-state.json", {**state, "status": "failed", "error": str(error)})
            raise
        ingest.atomic_json(run / "phase-state.json", {**state, "status": "complete"})


if __name__ == "__main__":
    main()
