"""Shrink a video to fit a Discord upload limit with ffmpeg (no GPU needed). The
planning and argument building are pure; only `shrink` touches the system."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Optional

from . import constants

log = logging.getLogger("red.tgfeed.transcode")


@dataclass
class TranscodePlan:
    video_kbps: int
    audio_kbps: int
    max_height: int


def plan_transcode(duration: float, limit_bytes: int, height: int = 0,
                   safety: float = constants.SIZE_SAFETY_FACTORS[0]) -> Optional[TranscodePlan]:
    """Pick a bitrate that should land under `limit_bytes`. None when the clip is so
    long that it would have to look terrible (or when the duration is unknown)."""
    if duration <= 0 or limit_bytes <= 0:
        return None
    total_kbps = limit_bytes * 8 * safety / 1000.0 / duration
    video_kbps = int(total_kbps - constants.AUDIO_KBPS)
    if video_kbps < constants.MIN_VIDEO_KBPS:
        return None
    video_kbps = min(video_kbps, constants.MAX_VIDEO_KBPS)
    if video_kbps < 500:
        cap = 480
    elif video_kbps < 1200:
        cap = 720
    else:
        cap = 1080
    max_height = min(height, cap) if height else cap
    return TranscodePlan(video_kbps=video_kbps, audio_kbps=constants.AUDIO_KBPS, max_height=max_height)


def build_ffmpeg_args(src: str, dst: str, plan: TranscodePlan, ffmpeg: str = "ffmpeg") -> list:
    """Re-encode to H.264/AAC mp4 capped at the plan's bitrate; all metadata dropped."""
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-map_metadata", "-1",
        "-vf", f"scale=-2:min(ih\\,{plan.max_height})",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", f"{plan.video_kbps}k", "-maxrate", f"{int(plan.video_kbps * 1.25)}k",
        "-bufsize", f"{plan.video_kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{plan.audio_kbps}k",
        "-movflags", "+faststart",
        dst,
    ]


async def probe_duration(path: str, ffprobe: str = "ffprobe") -> float:
    exe = shutil.which(ffprobe)
    if not exe:
        return 0.0
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def shrink(src: str, dst: str, duration: float, height: int, limit_bytes: int) -> Optional[int]:
    """Re-encode `src` to `dst` under `limit_bytes`. Returns the final size, or None
    if ffmpeg is missing, the clip is too long to shrink watchably, or both attempts
    came out too big."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.warning("tgfeed: ffmpeg not found; cannot shrink a video")
        return None
    if duration <= 0:
        duration = await probe_duration(src)
    for safety in constants.SIZE_SAFETY_FACTORS:
        plan = plan_transcode(duration, limit_bytes, height, safety)
        if plan is None:
            return None
        args = build_ffmpeg_args(src, dst, plan, ffmpeg)
        nice = shutil.which("nice")
        if nice:
            args = [nice, "-n", "10"] + args
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), constants.TRANSCODE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("tgfeed: ffmpeg timed out")
            _silent_remove(dst)
            return None
        if proc.returncode != 0:
            log.warning("tgfeed: ffmpeg failed (%s): %s", proc.returncode, (err or b"")[-300:].decode(errors="replace"))
            _silent_remove(dst)
            return None
        size = os.path.getsize(dst)
        if size <= limit_bytes:
            return size
        _silent_remove(dst)
    return None


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
