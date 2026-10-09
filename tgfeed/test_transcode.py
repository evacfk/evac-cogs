import asyncio
import os
import shutil
import subprocess

import pytest

from tgfeed import transcode


def test_plan_lands_under_the_limit():
    plan = transcode.plan_transcode(duration=60, limit_bytes=10 * 1024 * 1024, height=1080)
    assert plan is not None
    total_bits = (plan.video_kbps + plan.audio_kbps) * 1000 * 60
    assert total_bits / 8 < 10 * 1024 * 1024


def test_plan_refuses_clips_too_long_to_watch():
    assert transcode.plan_transcode(duration=3600, limit_bytes=10 * 1024 * 1024) is None


def test_plan_needs_a_duration():
    assert transcode.plan_transcode(0, 10_000_000) is None


def test_resolution_steps_down_with_bitrate():
    low = transcode.plan_transcode(180, 10 * 1024 * 1024, height=1080)
    high = transcode.plan_transcode(10, 10 * 1024 * 1024, height=1080)
    assert low.max_height < high.max_height
    assert transcode.plan_transcode(10, 10 * 1024 * 1024, height=360).max_height == 360


def test_args_drop_metadata_and_cap_bitrate():
    plan = transcode.TranscodePlan(video_kbps=800, audio_kbps=96, max_height=720)
    args = transcode.build_ffmpeg_args("in.mkv", "out.mp4", plan, ffmpeg="ffmpeg")
    assert args[0] == "ffmpeg" and args[-1] == "out.mp4"
    assert "-map_metadata" in args and args[args.index("-map_metadata") + 1] == "-1"
    assert "800k" in args and "1000k" in args and "-nostdin" in args


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_ffmpeg_shrinks_a_clip_under_the_limit_and_strips_metadata(tmp_path):
    src, dst = str(tmp_path / "in.mp4"), str(tmp_path / "out.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "20", "-c:v", "libx264", "-b:v", "6M",
         "-c:a", "aac", "-shortest", "-metadata", "title=SECRET-TITLE", "-metadata", "artist=SECRET-ARTIST", src],
        check=True,
    )
    probe = lambda path: subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "default=nw=1", path],
        capture_output=True, text=True, check=True).stdout
    assert "SECRET-TITLE" in probe(src)          # the source really carries it
    limit = os.path.getsize(src) // 3
    final = asyncio.run(transcode.shrink(src, dst, duration=20, height=720, limit_bytes=limit))
    assert final is not None and final <= limit and os.path.getsize(dst) == final
    assert "SECRET" not in probe(dst)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_shrink_gives_up_on_an_impossible_target(tmp_path):
    src, dst = str(tmp_path / "in.mp4"), str(tmp_path / "out.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
                    "-t", "5", src], check=True)
    assert asyncio.run(transcode.shrink(src, dst, duration=5, height=240, limit_bytes=1000)) is None
    assert not os.path.exists(dst)
