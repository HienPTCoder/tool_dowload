#!/usr/bin/env python3
"""Web local Pinterest: dán link X -> tải video. Chỉ lắng nghe trên 127.0.0.1."""
import json
import mimetypes
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from urllib.parse import urlsplit, urlunsplit

def normalize(url):
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    allowed = host == "pin.it" or host == "pinterest.com" or host.endswith(".pinterest.com")
    if parts.scheme != "https" or not allowed or parts.username or parts.password or parts.port:
        raise ValueError("Chỉ nhận link HTTPS pinterest.com hoặc pin.it")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))

import re as _re


def safe_folder(name) -> str:
    """Tên thư mục người dùng nhập -> tên an toàn (không có / .. ký tự cấm). Rỗng -> ''."""
    name = _re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip().strip(".")
    return name[:80]


HOST, PORT = "127.0.0.1", 8778
OUT_DIR = Path("~/Downloads/Pinterest").expanduser()
OUT_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=4)
BATCH_LOCKS: dict[str, threading.Lock] = {}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov"}


def upscale_file(source: Path) -> Path | None:
    """Create a 2x Lanczos upscale beside a downloaded image/video."""
    suffix = source.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        target = source.with_name(f"{source.stem}_upscaled.png")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-vf", "scale=iw*2:ih*2:flags=lanczos", "-frames:v", "1", str(target)]
    elif suffix in VIDEO_EXTENSIONS:
        target = source.with_name(f"{source.stem}_upscaled.mp4")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-vf", "scale=iw*2:ih*2:flags=lanczos", "-map", "0:v:0", "-map", "0:a?",
               "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "aac",
               "-b:a", "192k", "-movflags", "+faststart", str(target)]
    else:
        return None
    if target.exists():
        target = target.with_name(f"{target.stem}_{uuid.uuid4().hex[:4]}{target.suffix}")
        cmd[-1] = str(target)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
    return target


def run_job(jid, mode, cookies, upscale=False):
    job = JOBS[jid]
    folder = OUT_DIR / job["batch"]
    folder.mkdir(parents=True, exist_ok=True)
    job["status"] = "downloading"
    cmd = [sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input",
           "--no-colors", "--retries", "2", "--http-timeout", "30",
           "-D", str(folder), "-o", "directory=[]"]
    if mode == "images":
        cmd += ["--filter", "extension in ('jpg','jpeg','png','webp','gif','avif')"]
    elif mode == "videos":
        cmd += ["--filter", "extension in ('mp4','webm','mkv','mov','m3u8')"]
    if cookies:
        cmd += ["--cookies-from-browser", cookies + "/pinterest.com"]
    cmd += [job["url"]]
    log = folder / f".download-{jid}.log"
    batch_lock = BATCH_LOCKS.setdefault(job["batch"], threading.Lock())
    try:
        # Serialize links in the same list so each job can report only its own new files.
        with batch_lock:
            before = {f for f in folder.iterdir() if f.is_file()}
            with log.open("w") as output:
                result = subprocess.run(cmd, stdout=output, stderr=subprocess.STDOUT, timeout=7200)
            files = [f.relative_to(OUT_DIR).as_posix() for f in folder.iterdir()
                     if f.is_file() and f not in before and f != log and f.suffix not in (".part", ".ytdl")]
            job["files"] = files
            if result.returncode:
                raise RuntimeError(log.read_text(errors="replace")[-1200:] or "Tải thất bại")
            if not files:
                raise RuntimeError("Không tìm thấy media phù hợp. Thử chọn Cả ảnh và video hoặc đăng nhập từ trình duyệt.")
            original_count = len(files)
            if upscale:
                originals = [folder / Path(f).name for f in files]
                generated = [upscale_file(f) for f in originals]
                files.extend(p.relative_to(OUT_DIR).as_posix() for p in generated if p)
            job.update(status="done", progress=100, title=f"Đã tải {original_count} tệp")
    except Exception as exc:
        job.update(status="error", error=str(exc)[-1200:])
    finally:
        log.unlink(missing_ok=True)


def add_jobs(urls: list[str], audio: bool, cookies: str | None, folder: str = "", upscale=False) -> dict:
    added, skipped, seen = [], [], set()
    with LOCK:
        active = {j["url"] for j in JOBS.values() if j["status"] in ("queued", "downloading")}
    custom = safe_folder(folder)
    batch = custom or datetime.now().strftime("%Y-%m-%d_%H-%M-%S_") + uuid.uuid4().hex[:6]
    batch_created = False
    for raw in urls:
        raw = raw.strip()
        if not raw:
            continue
        try:
            u = normalize(raw)
        except (ValueError, TypeError):
            skipped.append(raw)
            continue
        if u in seen or u in active:
            continue
        seen.add(u)
        if not batch_created:
            (OUT_DIR / batch).mkdir(parents=True, exist_ok=True)
            batch_created = True
        jid = uuid.uuid4().hex[:8]
        JOBS[jid] = {"batch": batch, "id": jid, "url": u, "status": "queued", "progress": 0,
                     "files": [], "title": "", "error": "", "t": time.time()}
        POOL.submit(run_job, jid, audio, cookies, upscale)
        added.append(jid)
    return {"added": len(added), "skipped": skipped, "batch": batch if added else None}


class Handler(BaseHTTPRequestHandler):
    server_version = "xdl-web"

    def log_message(self, *a):
        pass

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        path = urlparse(self.path).path
        if path == "/":
            data = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/jobs":
            jobs = sorted(JOBS.values(), key=lambda j: -j["t"])
            self._json({"jobs": jobs, "dir": str(OUT_DIR)})
        elif path.startswith("/files/"):
            relative = Path(unquote(path[len("/files/"):]))
            f = (OUT_DIR / relative).resolve()
            if not f.is_relative_to(OUT_DIR.resolve()):
                return self._json({"error": "bad path"}, 403)
            name = f.name
            if not f.is_file():
                return self._json({"error": "not found"}, 404)
            size = f.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'inline; filename="{name}"')
            self.end_headers()
            with open(f, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    self.wfile.write(chunk)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._json({"error": "json only"}, 415)  # chặn form CSRF từ trang khác
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        path = urlparse(self.path).path
        if path == "/api/download":
            urls = body.get("urls") or []
            if isinstance(urls, str):
                urls = urls.split()
            cookies = body.get("cookies") or None
            if cookies not in (None, "chrome", "safari", "firefox", "edge", "brave"):
                return self._json({"error": "bad browser"}, 400)
            if not isinstance(urls, list) or len(urls) > 500 or not all(isinstance(u, str) for u in urls):
                return self._json({"error": "Tối đa 500 URL mỗi danh sách"}, 400)
            if body.get("mode", "all") not in ("all", "images", "videos"):
                return self._json({"error": "bad mode"}, 400)
            self._json(add_jobs(urls, body.get("mode", "all"), cookies, body.get("folder") or "",
                                body.get("upscale") is True))
        elif path == "/api/open-folder":
            subprocess.Popen(["open", str(OUT_DIR)])
            self._json({"ok": True})
        elif path == "/api/clear":
            with LOCK:
                for k in [k for k, j in JOBS.items() if j["status"] in ("done", "error")]:
                    del JOBS[k]
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


PAGE = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pinterest Downloader</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--tx:#e8eaed;--mut:#8b93a1;--ac:#4c8dff;--ok:#3ecf8e;--er:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 20px}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--mut);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
textarea{width:100%;min-height:120px;background:#10131a;color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:10px;font:13px/1.5 ui-monospace,Menlo,monospace;resize:vertical}
textarea:focus{outline:2px solid var(--ac);border-color:transparent}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:12px}
label{color:var(--mut);display:flex;gap:6px;align-items:center;font-size:14px}
input[type=text]{background:#10131a;color:var(--tx);border:1px solid var(--line);border-radius:6px;padding:5px 8px;width:210px}
select{background:#10131a;color:var(--tx);border:1px solid var(--line);border-radius:6px;padding:4px 6px}
button{background:var(--ac);color:#fff;border:0;border-radius:8px;padding:9px 18px;font-size:14px;font-weight:600;cursor:pointer}
button.sec{background:transparent;border:1px solid var(--line);color:var(--tx);font-weight:500}
button:disabled{opacity:.5;cursor:default}.sp{flex:1}
.job{display:grid;grid-template-columns:1fr auto;gap:4px 12px;padding:12px 0;border-top:1px solid var(--line)}
.job:first-child{border-top:0}.u{font:12px ui-monospace,Menlo,monospace;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{grid-column:1/-1;height:5px;background:var(--line);border-radius:3px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ac);transition:width .3s}
.st{font-size:13px;white-space:nowrap}.done{color:var(--ok)}.error{color:var(--er)}.downloading,.queued{color:var(--ac)}
.err{grid-column:1/-1;color:var(--er);font-size:13px}a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
.files{grid-column:1/-1;font-size:13px;display:flex;gap:14px;flex-wrap:wrap}
.empty{color:var(--mut);text-align:center;padding:20px}.note{color:var(--mut);font-size:12px;margin-top:8px}
#msg{color:var(--mut);font-size:13px}
</style></head><body><main>
<h1>Pinterest Downloader</h1>
<p class="sub">Dán link pin hoặc board Pinterest, mỗi dòng một link. Chỉ tải nội dung bạn có quyền sử dụng.</p>
<div class="card">
<textarea id="urls" placeholder="https://www.pinterest.com/pin/123456789/&#10;https://www.pinterest.com/user/board/" autofocus></textarea>
<div class="row">
<label>Loại media <select id="mode"><option value="all">Cả ảnh và video</option><option value="images">Chỉ ảnh</option><option value="videos">Chỉ video</option></select></label>
<label>Đăng nhập từ
<select id="cookies"><option value="">Không</option><option>chrome</option><option>safari</option><option>firefox</option><option>edge</option><option>brave</option></select></label>
<label>Tên thư mục <input id="folder_name" type="text" placeholder="để trống = tự đặt tên" maxlength="80"></label>
<label><input id="upscale" type="checkbox"> Upscale 2×</label>
<span class="sp"></span><span id="msg"></span>
<button id="go">Tải xuống</button>
</div>
<div class="note">Dùng "Đăng nhập từ" khi video cần đăng nhập hoặc bị gắn nội dung nhạy cảm. Media trong danh sách được lưu trực tiếp vào thư mục này. Upscale 2× tạo thêm bản phóng lớn bằng Lanczos (không khôi phục chi tiết đã mất), cần cài ffmpeg. Để trống tên, công cụ sẽ tự tạo thư mục riêng.</div>
</div>
<div class="card"><div class="row" style="margin:0 0 8px">
<strong>Danh sách tải</strong><span class="sp"></span>
<button class="sec" id="folder">Mở thư mục</button><button class="sec" id="clear">Xóa mục đã xong</button></div>
<div id="list"><div class="empty">Chưa có video nào.</div></div>
<div class="note" id="dir"></div></div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const post=(p,b)=>fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
const LBL={queued:'Đang chờ',downloading:'Đang tải',done:'Xong',error:'Lỗi'};
$('go').onclick=async()=>{
  const urls=$('urls').value.split(/\s+/).filter(Boolean);
  if(!urls.length){$('msg').textContent='Hãy dán ít nhất một link.';return}
  $('go').disabled=true;
  let r; try { r=await post('/api/download',{urls,mode:$('mode').value,cookies:$('cookies').value,folder:$('folder_name').value,upscale:$('upscale').checked}); } catch(e) { $('msg').textContent='Không kết nối được server'; $('go').disabled=false; return; }
  $('go').disabled=false;
  if(r.error){$('msg').textContent=r.error;return;}
  let m=`Đã thêm ${r.added} link.`; if(r.skipped&&r.skipped.length)m+=` Bỏ qua ${r.skipped.length} link Pinterest không hợp lệ.`;
  if(r.batch)m+=` Thư mục: ${r.batch}`;
  $('msg').textContent=m; if(r.added)$('urls').value=''; refresh();
};
$('folder').onclick=()=>post('/api/open-folder');
$('clear').onclick=async()=>{await post('/api/clear');refresh()};
async function refresh(){
  const d=await (await fetch('/api/jobs')).json();
  $('dir').textContent='Thư mục lưu: '+d.dir;
  if(!d.jobs.length){$('list').innerHTML='<div class="empty">Chưa có video nào.</div>';return}
  $('list').innerHTML=d.jobs.map(j=>`<div class="job">
   <div><div class="t">${esc(j.title||j.url)}</div>${j.title?`<div class="u">${esc(j.url)}</div>`:''}</div>
   <div class="st ${j.status}">${LBL[j.status]}${j.status==='downloading'?' '+j.progress+'%':''}</div>
   ${j.status==='downloading'||j.status==='queued'?`<div class="bar"><i style="width:${j.progress}%"></i></div>`:''}
   ${j.batch?`<div class="u">Thư mục: ${esc(j.batch)}</div>`:''}
   ${j.error?`<div class="err">${esc(j.error)}</div>`:''}
   ${j.files.length?`<div class="files">${j.files.map(f=>`<a href="/files/${f.split('/').map(encodeURIComponent).join('/')}" target="_blank">Xem</a><a href="/files/${f.split('/').map(encodeURIComponent).join('/')}" download="${esc(f)}">Lưu: ${esc(f.length>40?f.slice(0,37)+'…':f)}</a>`).join('')}</div>`:''}
  </div>`).join('');
}
setInterval(refresh,1000);refresh();
</script></body></html>"""


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Pinterest Downloader chạy tại http://{HOST}:{PORT}  (Ctrl+C để dừng)\nLưu vào: {OUT_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
