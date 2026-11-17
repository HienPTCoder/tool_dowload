#!/usr/bin/env python3
"""Web local cho xdl: dán link X -> tải video. Chỉ lắng nghe trên 127.0.0.1."""
import json
import mimetypes
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yt_dlp
from xdl import X_URL, normalize
from quality import add_upscale_postprocessor, best_media_options

import re as _re


def safe_folder(name) -> str:
    """Tên thư mục người dùng nhập -> tên an toàn (không có / .. ký tự cấm). Rỗng -> ''."""
    name = _re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip().strip(".")
    return name[:80]


HOST, PORT = "127.0.0.1", 8777
OUT_DIR = Path("~/Downloads/X-Videos").expanduser()
OUT_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=4)


def run_job(jid: str, audio: bool, cookies: str | None, upscale: bool, media: str):
    job = JOBS[jid]
    folder = OUT_DIR / job["batch"]
    folder.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frags = d.get("fragment_count") or 0
            if frags:  # HLS: tính theo số đoạn
                job["progress"] = round(min(d.get("fragment_index") or 0, frags) / frags * 100, 1)
            elif total:
                job["progress"] = round(d["downloaded_bytes"] / total * 100, 1)
        elif d["status"] == "finished":
            job["progress"] = 100

    opts = {
        "outtmpl": str(folder / "%(uploader_id)s_%(id)s_%(title).60B.%(ext)s"),
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "retries": 5,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    if audio:
        opts["format"] = "ba/b"
    elif media == "image":
        # Ảnh bài đăng X được yt-dlp cung cấp dưới dạng thumbnails; tải ảnh gốc.
        opts["skip_download"] = True
        opts["writethumbnail"] = True
    else:
        opts.update(best_media_options())
        opts["writethumbnail"] = media == "both"
    if audio:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    job["status"] = "downloading"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            add_upscale_postprocessor(ydl, not audio and media != "image" and upscale)
            info = ydl.extract_info(job["url"], download=True)
            entries = info.get("entries") or [info]
            files = []
            for e in entries:
                for rd in e.get("requested_downloads") or []:
                    fp = rd.get("filepath")
                    if fp:
                        files.append(Path(fp).resolve().relative_to(OUT_DIR.resolve()).as_posix())
                for thumb in e.get("thumbnails") or []:
                    fp = thumb.get("filepath")
                    if fp:
                        rel = Path(fp).resolve().relative_to(OUT_DIR.resolve()).as_posix()
                        if rel not in files:
                            files.append(rel)
            job["files"] = files
            job["title"] = info.get("title") or ""
        job["progress"] = 100
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e).splitlines()[-1][:300]


def add_jobs(urls: list[str], audio: bool, cookies: str | None, folder: str = "", upscale: bool = True,
             media: str = "both") -> dict:
    added, skipped, seen = [], [], set()
    with LOCK:
        active = {j["url"] for j in JOBS.values() if j["status"] in ("queued", "downloading")}
    # nhập tên -> thư mục riêng; để trống -> thư mục cũ
    batch = safe_folder(folder)  # rỗng -> lưu thẳng vào thư mục mặc định (như cũ)
    for raw in urls:
        raw = raw.strip()
        if not raw:
            continue
        if not X_URL.match(raw):
            skipped.append(raw)
            continue
        u = normalize(raw)
        if u in seen or u in active:
            continue
        seen.add(u)
        jid = uuid.uuid4().hex[:8]
        JOBS[jid] = {"batch": batch, "id": jid, "url": u, "status": "queued", "progress": 0,
                     "files": [], "title": "", "error": "", "t": time.time()}
        POOL.submit(run_job, jid, audio, cookies, upscale, media)
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
            f = (OUT_DIR / unquote(path[len("/files/"):])).resolve()
            if not f.is_relative_to(OUT_DIR.resolve()):  # chặn ../
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
            media = body.get("media", "both")
            if media not in ("video", "image", "both"):
                return self._json({"error": "bad media type"}, 400)
            self._json(add_jobs(urls, bool(body.get("audio")), cookies, body.get("folder") or "",
                                bool(body.get("upscale", True)), media))
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
<title>X Video Downloader</title>
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
.upscale-option{display:flex;align-items:center;gap:7px;padding:8px 10px;border:1px solid #355a91;border-radius:8px;background:#141d2b;color:var(--tx);cursor:pointer;white-space:nowrap}
.upscale-option input{margin:0;accent-color:var(--ac);width:16px;height:16px;flex:none}
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
<h1>X Video Downloader</h1>
<p class="sub">Dán link bài đăng X (Twitter), mỗi dòng một link. Nhập "Thư mục mới" để lưu riêng, để trống thì lưu vào thư mục cũ. Chỉ tải nội dung bạn có quyền sử dụng.</p>
<div class="card">
<textarea id="urls" placeholder="https://x.com/user/status/123456789&#10;https://x.com/user/status/987654321" autofocus></textarea>
<div class="row">
<label><input type="checkbox" id="audio"> Chỉ lấy âm thanh (mp3)</label>
<label>Tải về
<select id="media"><option value="video">Video</option><option value="image">Ảnh</option><option value="both" selected>Video + ảnh</option></select></label>
<label>Đăng nhập từ
<select id="cookies"><option value="">Không</option><option>chrome</option><option>safari</option><option>firefox</option><option>edge</option><option>brave</option></select></label>
<label>Thư mục mới <input id="folder_name" type="text" placeholder="để trống = lưu thư mục cũ" maxlength="80"></label>
<span class="sp"></span><span id="msg"></span>
<label class="upscale-option" for="upscale"><input type="checkbox" id="upscale" checked> Upscale 2x</label>
<button id="go">Tải xuống</button>
</div>
<div class="note">Chọn định dạng video và thumbnail có độ phân giải cao nhất nguồn cung cấp. Upscale 2x dùng Lanczos, tăng kích thước điểm ảnh chứ không khôi phục chi tiết đã mất. Dùng "Đăng nhập từ" khi video cần đăng nhập hoặc bị gắn nội dung nhạy cảm. Tối đa 4 video tải cùng lúc.</div>
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
  const r=await post('/api/download',{urls,audio:$('audio').checked,media:$('media').value,upscale:$('upscale').checked,cookies:$('cookies').value,folder:$('folder_name').value});
  $('go').disabled=false;
  let m=`Đã thêm ${r.added} link.`; if(r.skipped&&r.skipped.length)m+=` Bỏ qua ${r.skipped.length} link không hợp lệ.`;
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
    print(f"X Video Downloader chạy tại http://{HOST}:{PORT}  (Ctrl+C để dừng)\nLưu vào: {OUT_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
