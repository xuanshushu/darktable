"""Exercise collision, repeat and cleanup invariants on disposable local files."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import struct
import stat
import tempfile
import sys
from types import SimpleNamespace
sys.dont_write_bytecode = True
import ingest


def main():
    scratch_parent = Path(tempfile.gettempdir()).resolve()
    temporary = tempfile.TemporaryDirectory(prefix="camera-ingest-check-", dir=scratch_parent)
    scratch = Path(temporary.name).resolve()
    assert scratch.parent == scratch_parent
    try:
        destination = scratch / "nas"
        destination.mkdir()
        run = scratch / "run"
        source = run / "staging" / "2026-10-10" / "DSC00001.ARW"
        source.parent.mkdir(parents=True)
        date = b"2026:10:11 10:30:00\x00"
        photo = b"II"+struct.pack("<HI",42,8)+struct.pack("<H",1)+struct.pack("<HHII",0x9003,2,len(date),26)+struct.pack("<I",0)+date+b"raw-data"*100
        source.write_bytes(photo)
        manifest = {"camera_id": "TEST-CAMERA", "camera_name": "Test", "storage_name": "CARD", "files": [
            {"source_path": "2026-10-10/DSC00001.ARW", "day": "2026-10-10", "name": "DSC00001.ARW", "size": len(photo)}]}
        manifest_path = run / "source.json"
        ingest.atomic_json(manifest_path, manifest)
        args = SimpleNamespace(manifest=str(manifest_path), dest=str(destination), run=str(run), seed=None)
        with redirect_stdout(io.StringIO()):
            ingest.plan(args)
        request = ingest.load(run / "plan.json")
        item = request["files"][0]
        assert item["action"] == "copy"
        row = ingest.copy_photo(request, item, run)
        target = destination / row["record"]["target_rel"]
        assert target.read_bytes() == photo and row["record"]["target_rel"].startswith("2026/2026-10-11/")
        ingest.ledger_merge(destination, {row["key"]: row["record"]})
        with redirect_stdout(io.StringIO()):
            ingest.plan(args)
        repeated = ingest.load(run / "plan.json")
        assert repeated["files"][0]["action"] == "cached" and repeated["summary"]["copy_bytes"] == 0
        foreign = b"x"*len(photo)
        target.write_bytes(foreign)
        os.utime(target, ns=(row["record"]["target_mtime_ns"]+2000000000,)*2)
        with redirect_stdout(io.StringIO()):
            ingest.plan(args)
        collision = ingest.load(run / "plan.json")
        assert collision["files"][0]["action"] == "conflict"
        preserved = ingest.copy_photo(collision, collision["files"][0], run)
        correct = destination / preserved["record"]["target_rel"]
        assert target.read_bytes() == foreign and correct.read_bytes() == photo and correct != target
        ingest.ledger_merge(destination, {preserved["key"]: preserved["record"]})
        correct.unlink()
        with redirect_stdout(io.StringIO()):
            ingest.plan(args)
        missing = ingest.load(run / "plan.json")
        assert missing["files"][0]["action"] == "conflict" and missing["files"][0]["reason"] == "recorded_target_missing"
        restored = ingest.copy_photo(missing, missing["files"][0], run)
        record = restored["record"]
        record.update(state="cataloged", catalogs={"test": {"imgid": 1}})
        ingest.ledger_merge(destination, {restored["key"]: record})
        cleanup_args = SimpleNamespace(plan=str(run / "plan.json"), run=str(run), catalog_id="test")
        source.write_bytes(photo+b"changed")
        try:
            ingest.cleanup(cleanup_args)
        except RuntimeError:
            pass
        else:
            raise AssertionError("cleanup accepted changed staging")
        source.write_bytes(photo)
        os.utime(source, ns=(record["staging_mtime_ns"],)*2)
        if os.name == "nt":
            os.chmod(source, stat.S_IREAD)
        with redirect_stdout(io.StringIO()):
            ingest.cleanup(cleanup_args)
        assert not source.exists() and (destination / record["target_rel"]).read_bytes() == photo and target.read_bytes() == foreign
        for value in ("../outside.ARW", "C:/outside.ARW", "/outside.ARW"):
            try:
                ingest.safe_relative(value)
            except ValueError:
                pass
            else:
                raise AssertionError("accepted unsafe path")
        print("passed: date routing, repeat skip, collision preservation, missing target recovery, guarded cleanup, path containment")
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    main()
