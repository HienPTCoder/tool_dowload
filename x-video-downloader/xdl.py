#!/usr/bin/env python3
"""xdl - tải video từ X (Twitter) bằng URL.

Dùng:
    xdl.py URL [URL ...]
    xdl.py -f links.txt -j 6                   # tải hàng loạt, 6 link cùng lúc
    xdl.py URL -o ~/Downloads/X --audio
    xdl.py URL --cookies-from-browser chrome   # video cần đăng nhập / nhạy cảm

Chỉ tải nội dung bạn có quyền sử dụng.
"""
import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import yt_dlp
    from quality import add_upscale_postprocessor, best_media_options
except ImportError:
    sys.exit("Thiếu yt-dlp hoặc module quality. Chạy qua ./xdl (dùng .venv).")

X_URL = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter|fxtwitter|vxtwitter|fixupx)\.com/"
    r"[^/]+/status(?:es)?/\d+",
    re.I,
)


def normalize(url: str) -> str:
    """Đưa link mirror (fxtwitter...) về x.com và bỏ tham số theo dõi."""
    url = url.strip().split("?")[0].split("#")[0]
    url = re.sub(r"(/status(?:es)?/\d+).*$", r"\1", url, flags=re.I)  # bỏ /video/1, /photo/1...
    return re.sub(r"(?:fxtwitter|vxtwitter|fixupx|mobile\.twitter|twitter)\.com",
                  "x.com", url, flags=re.I)


def read_urls(args) -> list[str]:
    urls = list(args.urls)
    if args.file:
        for line in Path(args.file).expanduser().read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    seen, out = set(), []
    for u in urls:
        if not X_URL.match(u):
            print(f"[bỏ qua] không phải link bài đăng X: {u}", file=sys.stderr)
            continue
        n = normalize(u)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Tải video từ X (Twitter) bằng URL")
    p.add_argument("urls", nargs="*", help="link bài đăng, ví dụ https://x.com/user/status/123")
    p.add_argument("-f", "--file", help="file txt, mỗi dòng một link")
    p.add_argument("-o", "--output", default="~/Downloads/X-Videos", help="thư mục lưu")
    p.add_argument("--no-subfolder", action="store_true", help="lưu thẳng vào thư mục -o, không tạo thư mục con theo giờ")
    p.add_argument("--audio", action="store_true", help="chỉ lấy âm thanh (mp3)")
    p.add_argument("--no-upscale", action="store_true", help="không upscale video sau khi tải")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="dùng đăng nhập của trình duyệt (chrome, safari, firefox...)")
    p.add_argument("--info", action="store_true", help="chỉ in thông tin, không tải")
    p.add_argument("-j", "--jobs", type=int, default=4,
                   help="số link tải song song (mặc định 4; đặt 1 để tải lần lượt)")
    args = p.parse_args()

    urls = read_urls(args)
    if not urls:
        p.error("không có link X hợp lệ")

    out_dir = Path(args.output).expanduser()
    if not args.no_subfolder:  # mỗi lần chạy (một danh sách) -> một thư mục con riêng
        out_dir = out_dir / time.strftime("%Y-%m-%d_%H-%M-%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    opts = {
        "outtmpl": str(out_dir / "%(uploader_id)s_%(id)s_%(title).60B.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": False,          # bài có nhiều video -> tải hết
        "merge_output_format": "mp4",
        "retries": 5,
        "quiet": False,
        "noprogress": True,
        "no_warnings": False,
    }
    opts.update(best_media_options())
    if args.audio:
        opts["format"] = "ba/b"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)

    jobs = max(1, min(args.jobs, len(urls)))
    total = len(urls)
    lock = threading.Lock()
    done = 0

    def work(u: str):
        """Mỗi luồng dùng một YoutubeDL riêng (yt-dlp không an toàn khi dùng chung)."""
        nonlocal done
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                add_upscale_postprocessor(ydl, not args.audio and not args.no_upscale)
                if args.info:
                    info = ydl.extract_info(u, download=False)
                    msg = f"{info.get('uploader')} | {info.get('duration')}s | {info.get('title')}"
                else:
                    ydl.download([u])
                    msg = "đã tải"
            err = None
        except Exception as e:  # noqa: BLE001
            msg, err = "LỖI", str(e).splitlines()[-1]
        with lock:
            done += 1
            print(f"[{done}/{total}] {u}  ->  {err or msg}", flush=True)
        return u, err

    print(f"Bắt đầu {total} link, {jobs} luồng song song...", flush=True)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(work, urls))

    fail = [(u, e) for u, e in results if e]
    ok = total - len(fail)
    print(f"\nXong: {ok}/{total} thành công. Thư mục: {out_dir}")
    for u, err in fail:
        print(f"  LỖI {u}\n    {err}", file=sys.stderr)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
