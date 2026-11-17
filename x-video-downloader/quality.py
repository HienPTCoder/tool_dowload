"""Best available X media selection and optional local 2x video scaling."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from yt_dlp.postprocessor.common import PostProcessor


def best_media_options() -> dict:
    """Prefer the largest available video and the highest available thumbnail."""
    return {
        "format": "bestvideo*+bestaudio/best",
        "format_sort": ["res", "fps", "tbr"],
        "format_sort_force": True,
        # X exposes the original video thumbnail as the `orig` thumbnail variant.
        "writethumbnail": True,
    }


class VideoUpscalePP(PostProcessor):
    """Double video dimensions with Lanczos and mild sharpening after download.

    This is a conventional spatial upscale, not a generative super-resolution
    model; it increases pixel dimensions but cannot recover detail absent at source.
    """

    def run(self, info):
        filename = info.get("filepath")
        if not filename:
            return [], info

        source = Path(filename)
        if source.suffix.lower() not in {".mp4", ".m4v", ".mov", ".mkv"}:
            return [], info

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.report_warning("ffmpeg không có trong PATH; bỏ qua upscale video")
            return [], info

        temp = source.with_name(f"{source.stem}.upscale-tmp.mp4")
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", "scale=iw*2:ih*2:flags=lanczos,unsharp=5:5:0.35:3:3:0.0",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
            str(temp),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            os.replace(temp, source)
            self.to_screen(f"[upscale] Đã tăng kích thước video 2x: {source.name}")
        except (OSError, subprocess.CalledProcessError) as exc:
            temp.unlink(missing_ok=True)
            detail = getattr(exc, "stderr", "") or str(exc)
            self.report_warning(f"Không upscale được {source.name}: {detail[-300:]}")
        return [], info


def add_upscale_postprocessor(ydl, enabled: bool) -> None:
    if enabled:
        ydl.add_post_processor(VideoUpscalePP(ydl), when="after_move")
