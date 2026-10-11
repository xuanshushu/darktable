"""Exercise cache bounds, overlap and recovery with real local archival and simulated camera/catalog adapters."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
import ingest
import rolling


def photo_bytes(index):
    date = b"2026:10:11 10:30:00\x00"
    head = b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", 1)
    head += struct.pack("<HHII", 0x9003, 2, len(date), 26) + struct.pack("<I", 0) + date
    return head + bytes([index]) * (1024 - len(head))


def fixture(root, count=8):
    destination = root / "nas"
    destination.mkdir(parents=True)
    run = root / "run"
    sources = [{"source_path": "2026-10-10/DSC%05d.ARW" % n, "name": "DSC%05d.ARW" % n,
                "day": "2026-10-10", "size": 1024} for n in range(count)]
    manifest = {"camera_id": "TEST", "camera_name": "Test", "storage_name": "CARD", "files": sources}
    manifest_path = run / "source.json"
    ingest.atomic_json(manifest_path, manifest)
    ingest.plan(SimpleNamespace(manifest=str(manifest_path), dest=str(destination), run=str(run), seed=None))
    args = SimpleNamespace(plan=str(run / "plan.json"), run=str(run), cache_mib=4096/rolling.MIB,
        batch_mib=2048/rolling.MIB, workers=2, catalog_id="test", catalog_root=str(destination),
        gdbus="simulated", pwsh="simulated", camera_timeout=10, timeout=20)
    return args, destination


def adapters():
    state = {"copies": [], "catalog": set(), "overlap": False, "fail_import": False, "peak": 0}
    lock = threading.Lock()

    def transfer(pipeline, batch, files):
        for item in files:
            path = ingest.inside(batch.root / "staging", item["source_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                with lock:
                    state["copies"].append(item["key"])
                    state["overlap"] |= any(b.index < batch.index and b.state not in {"waiting", "complete"} for b in pipeline.batches)
                path.write_bytes(photo_bytes(int(item["name"][3:8])))
                time.sleep(0.03)
            ready = batch.root / "ready" / (item["key"] + ".ready")
            ready.parent.mkdir(exist_ok=True)
            ready.write_text(item["source_path"], encoding="utf-8")
            with lock:
                live = sum(size for b in pipeline.batches for _, size in pipeline.cache_files(b))
                state["peak"] = max(state["peak"], live)
                assert live <= pipeline.cache_bytes

    def catalog(args):
        if state["fail_import"]:
            raise RuntimeError("simulated darktable unavailable")
        request = ingest.load(args.plan)
        ledger = ingest.ledger_load(request["destination"])["entries"]
        before = len(state["catalog"])
        added = existing = 0
        updates = {}
        rows = []
        for item in request["files"]:
            record = dict(ledger[item["key"]])
            target = ingest.inside(Path(request["destination"]), record["target_rel"])
            assert target.read_bytes() == photo_bytes(int(item["name"][3:8]))
            if item["key"] in state["catalog"]:
                existing += 1
                status = "existing"
            else:
                added += 1
                status = "imported"
                state["catalog"].add(item["key"])
            record.update(state="cataloged", catalogs={"test": {"imgid": len(state["catalog"])}})
            updates[item["key"]] = record
            rows.append("\t".join([item["key"], status, "1", str(target), "2026:10:11 10:30:00"]))
        ingest.ledger_merge(request["destination"], updates)
        Path(args.run, "catalog-results.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        ingest.atomic_json(Path(args.run) / "catalog-summary.json", {"before": before, "after": len(state["catalog"]),
                                                                  "imported": added, "existing": existing, "failed": 0})
    return state, transfer, catalog


def main():
    parent = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="rolling-ingest-check-", dir=parent) as name:
        root = Path(name).resolve()
        assert root.parent == parent
        with redirect_stdout(io.StringIO()):
            args, destination = fixture(root / "bounded")
            state, transfer, catalog = adapters()
            with patch.object(rolling.Pipeline, "transfer_batch", transfer), patch.object(ingest, "import_catalog", catalog):
                pipeline = rolling.Pipeline(args)
                pipeline.execute()
                summary = ingest.load(Path(args.run) / "rolling-summary.json")
                assert summary["catalog"]["imported"] == 8 and summary["archived"] == 8
                assert summary["staged"] == 8 and summary["cache_bytes"] == 0
                assert summary["peak_reserved_bytes"] <= 4096 and 0 < state["peak"] <= 4096
                assert state["overlap"] and len(state["copies"]) == 8
                assert all((destination / x["target_rel"]).is_file() for x in ingest.ledger_load(destination)["entries"].values())
                original_copies = list(state["copies"])
                rolling.Pipeline(args).execute()
                assert state["copies"] == original_copies
                resumed_summary = ingest.load(Path(args.run) / "rolling-summary.json")
                assert resumed_summary["peak_reserved_bytes"] == summary["peak_reserved_bytes"]
            request = ingest.load(args.plan)
            try:
                rolling.split_batches(request, 512, 1000)
            except ValueError:
                pass
            else:
                raise AssertionError("oversized photo accepted")
            groups = rolling.split_batches(request, 512, 2048)
            assert len(groups) == 8 and all(len(group) == 1 for group in groups)
            changed = SimpleNamespace(**{**vars(args), "cache_mib": 8192/rolling.MIB})
            try:
                rolling.Pipeline(changed)
            except ValueError:
                pass
            else:
                raise AssertionError("resume accepted changed cache settings")

            args, destination = fixture(root / "retry")
            state, transfer, catalog = adapters()
            state["fail_import"] = True
            with patch.object(rolling.Pipeline, "transfer_batch", transfer), patch.object(ingest, "import_catalog", catalog):
                failed = rolling.Pipeline(args)
                try:
                    failed.execute()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("catalog failure was ignored")
                retained = [path for batch in failed.batches for path, _ in failed.cache_files(batch)]
                assert retained and all(path.exists() for path in retained)
                assert sum(path.stat().st_size for path in retained) <= 4096
                archived_keys = set()
                for batch in failed.batches:
                    journal = batch.root / "archive-results.jsonl"
                    if journal.exists():
                        archived_keys.update(json.loads(line)["key"] for line in journal.read_text(encoding="utf-8").splitlines())
                assert archived_keys
                before = len(state["copies"])
                state["fail_import"] = False
                resumed = rolling.Pipeline(args)
                resumed.execute()
                assert archived_keys.isdisjoint(state["copies"][before:])
                assert len(state["catalog"]) == 8
                assert not any(resumed.cache_files(batch) for batch in resumed.batches)

            args, destination = fixture(root / "camera-failure", count=2)
            state, transfer, catalog = adapters()
            def disconnected(pipeline, batch, files):
                transfer(pipeline, batch, files[:1])
                raise RuntimeError("simulated camera disconnected")
            with patch.object(rolling.Pipeline, "transfer_batch", disconnected), patch.object(ingest, "import_catalog", catalog):
                failed = rolling.Pipeline(args)
                try:
                    failed.execute()
                except RuntimeError as error:
                    assert "camera disconnected" in str(error)
                else:
                    raise AssertionError("camera failure was ignored")
                assert not state["catalog"]
                assert any(failed.cache_files(batch) for batch in failed.batches)

            lock_root = root / "lock"
            lock_root.mkdir()
            with rolling.run_lock(lock_root):
                try:
                    with rolling.run_lock(lock_root):
                        pass
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("concurrent run was allowed")
        print("passed: bounded cache, camera/NAS overlap, per-batch cleanup, completed-run resume, failed-catalog retention, retry without camera reread, oversized-file refusal, run lock")


if __name__ == "__main__":
    main()
