"""Serve a read-only ingest dashboard on loopback; no photo contents are served."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
import sys
sys.dont_write_bytecode = True
from ingest import transfer_ready

PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>相机导入进度</title><style>
*{box-sizing:border-box}body{margin:0;background:#10151c;color:#eef3f8;font:16px system-ui,"Microsoft YaHei",sans-serif}main{max-width:880px;margin:auto;padding:32px 24px}h1{font-size:27px;margin:0 0 9px}.muted{color:#aab7c6;font-size:14px;line-height:1.7}.pill{display:inline-block;background:#223544;color:#b5e8f3;border-radius:20px;padding:5px 12px;font-size:13px;margin-bottom:20px}.card{background:#1a222d;border:1px solid #2a3543;border-radius:16px;padding:22px;margin:16px 0}.row{display:flex;justify-content:space-between;gap:16px;align-items:center}.title{font-weight:650}.count{font-variant-numeric:tabular-nums;color:#c8dded}.track{height:10px;background:#303c4b;border-radius:10px;margin:18px 0 12px;overflow:hidden}.bar{height:100%;width:0;background:#55c6aa;transition:width 1s}.nas .bar{background:#76b6f3}.catalog .bar{background:#b8a1f6}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}.stat{background:#1a222d;border-radius:12px;padding:17px}.big{font-size:25px;margin-top:8px;font-variant-numeric:tabular-nums}footer{font-size:13px;color:#8495a8;line-height:1.8;margin-top:24px}.error{color:#ffc089} @media(max-width:560px){main{padding:24px 16px}.stats{grid-template-columns:1fr}.big{font-size:22px}.row{align-items:flex-start}}
</style><main><div class="pill" id="badge">读取进度中</div><h1>Sony → NAS → darktable</h1>
<div class="muted">每 2 秒自动更新，可随时切回其他工作。相机原片保留。</div>
<div class="stats"><div class="stat"><div class="muted">NAS 当前归档速度</div><div class="big" id="speed">—</div></div><div class="stat"><div class="muted">预计 NAS 剩余时间</div><div class="big" id="eta">—</div></div><div class="stat"><div class="muted">已归档数据 / 本次待处理</div><div class="big" id="bytes">—</div></div></div>
<div class="card"><div class="row"><span class="title">① 相机传输到本地暂存</span><span class="count" id="camera-count">—</span></div><div class="track"><div class="bar" id="camera-bar"></div></div><div class="muted" id="camera-text">正在读取相机文件目录</div><div class="muted" id="cache-text"></div></div>
<div class="card nas"><div class="row"><span class="title">② NAS 按拍摄日期归档</span><span class="count" id="nas-count">—</span></div><div class="track"><div class="bar" id="nas-bar"></div></div><div class="muted" id="nas-text">—</div></div>
<div class="card catalog"><div class="row"><span class="title">③ 批量登记到 darktable</span><span class="count" id="catalog-count">等待首批归档</span></div><div class="track"><div class="bar" id="catalog-bar"></div></div><div class="muted" id="catalog-text">每批入库成功后释放本地缓存</div></div>
<footer id="footer">—</footer></main><script>
const el=id=>document.getElementById(id), gib=n=>(n/1073741824).toFixed(1)+' GiB';
const size=n=>n<1073741824?(n/1048576).toFixed(0)+' MiB':gib(n);
function duration(n){if(!n||!Number.isFinite(n))return '计算中';return n<60?'不足 1 分钟':Math.ceil(n/60)+' 分钟左右'}
function bar(id,n,t){el(id+'-bar').style.width=(t?Math.min(100,n/t*100):100)+'%'}
async function update(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();
el('badge').textContent=s.state&&s.state.status==='failed'?'导入暂停，等待处理':s.catalog_finished?(s.state&&s.state.test_source?'本次验证完成':'导入完成'):s.rolling&&s.archived<s.pending?'正在滚动导入':s.catalog||(s.state&&s.state.phase==='import')?'正在登记到 darktable':s.stale?'NAS 进度暂未更新':s.archived===s.pending?'NAS 归档完成，等待入库':'正在导入';
el('speed').textContent=s.archived===s.pending?'已完成':s.speed?(s.speed/1000000).toFixed(1)+' MB/s':'计算中';el('eta').textContent=s.archived===s.pending?'已完成':duration(s.eta);
el('bytes').textContent=gib(s.archive_bytes)+' / '+gib(s.total_bytes);
el('camera-count').textContent=s.staged+' / '+s.pending;bar('camera',s.staged,s.pending);
el('camera-text').textContent='累计已传输 '+gib(s.staged_bytes)+'；历史记录已跳过 '+s.skipped+' 张';
el('cache-text').textContent=s.rolling?'当前本地缓存 '+size(s.cache_bytes)+'；已预留 '+size(s.reserved_bytes)+' / 上限 '+size(s.cache_limit_bytes)+'；每批入库后自动释放':'';
el('nas-count').textContent=(s.skipped+s.archived)+' / '+s.total;bar('nas',s.skipped+s.archived,s.total);
el('nas-text').textContent='本次已归档 '+s.archived+' / '+s.pending+' 张；写入时计算 SHA-256，按字节数检查完成';
if(s.catalog){el('catalog-count').textContent=(s.catalog.imported+s.catalog.existing)+' / '+s.total;bar('catalog',s.catalog.imported+s.catalog.existing,s.total);el('catalog-text').textContent=s.catalog.failed?'有 '+s.catalog.failed+' 张登记失败，等待处理':s.catalog_finished?'全部登记完成：'+s.catalog.imported+' 张新增，'+s.catalog.existing+' 张已在照片库':'正在批量登记：'+s.catalog.imported+' 张新增，'+s.catalog.existing+' 张已在照片库';}
el('footer').textContent=s.state&&s.state.status==='failed'?'失败原因：'+s.state.error:(s.state&&s.state.test_source?s.state.test_source+' · ':'')+'最后更新：'+new Date(s.now*1000).toLocaleTimeString()+' · 相机 '+s.camera+' · 预计时间按近期归档速度计算，会随网络波动变化';
}catch(e){el('badge').textContent='连接暂时中断，保留最后进度';el('badge').classList.add('error')}}update();setInterval(update,2000);
</script></html>'''


def read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    run = Path(args.run).resolve()
    plan = read(run / "plan.json", None)
    if not plan:
        raise SystemExit("plan.json is required")
    pending = [x for x in plan["files"] if x["action"] in {"copy", "conflict"}]
    recent = []
    staged_keys = set()

    def status():
        rolling = read(run / "rolling-progress.json", None)
        archive = read(run / "archive-progress.json", {})
        staged = staged_bytes = 0
        for item in ([] if rolling else pending):
            try:
                path = run / "staging" / item["source_path"]
                if item["key"] not in staged_keys and transfer_ready(path, item["size"]):
                    staged_keys.add(item["key"])
                if item["key"] in staged_keys:
                    staged += 1
                    staged_bytes += item["size"]
            except OSError:
                pass
        now = time.time()
        count = archive.get("completed", 0)
        byte_count = archive.get("bytes", 0)
        if rolling:
            staged, staged_bytes = rolling["staged"], rolling["staged_bytes"]
            count, byte_count = rolling["archived"], rolling["archive_bytes"]
        recent.append((now, byte_count))
        while len(recent) > 1 and now - recent[0][0] > 65:
            recent.pop(0)
        speed = archive.get("bytes_per_second", 0)
        if len(recent) > 1 and now - recent[0][0] >= 15:
            speed = (byte_count - recent[0][1]) / (now - recent[0][0])
        total_bytes = sum(x["size"] for x in pending)
        cleanup = read(run / "cleanup-summary.json", {})
        if cleanup.get("removed", 0) == len(pending):
            staged = len(pending)
            staged_bytes = total_bytes
        catalog = read(run / "catalog-summary.json", None)
        catalog_finished = catalog is not None and catalog.get("failed", 0) == 0
        if rolling:
            catalog, catalog_finished = rolling["catalog"], rolling["catalog_finished"]
        if catalog is None and (run / "catalog-results.tsv").exists():
            catalog = {"imported": 0, "existing": 0, "failed": 0}
            for line in (run / "catalog-results.tsv").read_text(encoding="utf-8").splitlines():
                fields = line.split("\t")
                if len(fields) == 5 and fields[1] in catalog:
                    catalog[fields[1]] += 1
        progress_path = run / ("rolling-progress.json" if rolling else "archive-progress.json")
        return {"now": now, "camera": plan.get("camera_name", "camera"),
                "total": len(plan["files"]), "pending": len(pending),
                "skipped": len(plan["files"]) - len(pending), "staged": staged,
                "staged_bytes": staged_bytes, "archived": count,
                "archive_bytes": byte_count, "total_bytes": total_bytes,
                "speed": speed, "eta": (total_bytes-byte_count)/speed if speed else None,
                "catalog": catalog, "catalog_finished": catalog_finished,
                "rolling": rolling is not None, "cache_bytes": rolling.get("cache_bytes", 0) if rolling else None,
                "reserved_bytes": rolling.get("reserved_bytes", 0) if rolling else None,
                "cache_limit_bytes": rolling.get("cache_limit_bytes", 0) if rolling else None,
                "state": read(run / "phase-state.json", None),
                "stale": count < len(pending) and (not progress_path.exists()
                           or now-progress_path.stat().st_mtime > 45)}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/status":
                body = json.dumps(status(), ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path == "/":
                body = PAGE.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("http://127.0.0.1:" + str(server.server_port) + "/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
