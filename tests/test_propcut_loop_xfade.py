"""propcut 循环消接缝（P1-3）——曲长 < 片长时 crossfade 拼贴替代 -stream_loop 硬接。

覆盖：music_loop_plan 决策边界 / filter 图拼装（含拼贴总长够不够的数学）/
真 ffmpeg 端到端：3s 短曲配 10s 片，成片尾部仍有声（份数算短了 = 尾部静音，
这是本机制唯一真实翻车面）+ 与 loudnorm 组合仍归一到位。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from estatecut.ffmpeg_tools import ffmpeg_available

from propcut.export import (build_music_audio_chain, export_video, music_loop_plan)


def _music_cfg(**over) -> dict:
    cfg = {"original_audio": "remove", "volume": 0.8, "fade_in": 0.0, "fade_out": 2.0,
           "music_start": 0.0, "loudnorm": {"enabled": False}}
    cfg.update(over)
    return cfg


# ---- 决策边界 ----

def test_loop_plan_boundaries() -> None:
    assert music_loop_plan(None, 0.0, 30.0) is None            # 曲长未知 → 现状
    assert music_loop_plan(60.0, 0.0, 30.0) is None            # 曲够长，播不到曲尾
    assert music_loop_plan(2.0, 0.0, 30.0) is None             # 曲太短，没料可交叉
    assert music_loop_plan(30.0, 29.5, 10.0) is None           # music_start 吃掉整曲
    assert music_loop_plan(3.5, 0.0, 3600.0) is None           # 病态组合超份数帽 → 现状

    plan = music_loop_plan(3.0, 0.5, 10.0)
    assert plan is not None
    copies, xfade = plan
    assert xfade == 0.75                                        # min(1.0, 3/4)
    # 总长 N*L-(N-1)*d 必须盖住 music_start + target + d + 0.5
    assert copies * 3.0 - (copies - 1) * xfade >= 0.5 + 10.0 + xfade + 0.5


def test_loop_plan_total_length_always_sufficient() -> None:
    for length in (3.0, 4.5, 7.0, 12.0, 29.0):
        for target in (8.0, 15.0, 33.0, 61.0, 180.0):
            for start in (0.0, 1.5):
                plan = music_loop_plan(length, start, target)
                if plan is None:
                    xfade = min(1.0, length / 4)
                    over_cap = (start + target + 0.5) / (length - xfade) > 64
                    assert length - start >= target + 0.25 or length < 3.0 or over_cap, \
                        (length, target, start)
                    continue
                copies, xfade = plan
                total = copies * length - (copies - 1) * xfade
                assert total >= start + target, (length, target, start, plan)


# ---- filter 图 ----

def test_chain_engages_crossfade_only_when_looping() -> None:
    cfg = _music_cfg(music_start=0.5, fade_in=0.5)
    looped = build_music_audio_chain(cfg, 10.0, track_dur=3.0)
    assert "asplit=" in looped and "acrossfade=d=0.750" in looped
    assert "[loop]atrim=start=0.500" in looped            # 循环流之后才走原有起点/音量步骤
    copies, _ = music_loop_plan(3.0, 0.5, 10.0)
    assert looped.count("acrossfade") == copies - 1
    # 曲够长 / 不传曲长 → 与现状逐字符一致（零行为变化）
    plain = build_music_audio_chain(cfg, 10.0)
    assert build_music_audio_chain(cfg, 10.0, track_dur=60.0) == plain
    assert "acrossfade" not in plain


def test_chain_mix_mode_keeps_amix_shape() -> None:
    chain = build_music_audio_chain(_music_cfg(original_audio="mix"), 10.0,
                                    mix_original=True, track_dur=3.0)
    assert chain.count("acrossfade") >= 1
    assert "amix=inputs=2" in chain and chain.endswith("[outa]")


# ---- 真 ffmpeg 端到端 ----

def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def _tail_mean_volume(path: Path, tail_start: float) -> float:
    """成片尾段 volumedetect mean_volume（dB）——拼贴份数不够时这里是纯静音。"""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{tail_start:.3f}",
                           "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", shell=False)
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", proc.stderr)
    assert m, proc.stderr[-500:]
    return float(m.group(1))


def test_export_short_track_loops_seamlessly(tmp_path: Path) -> None:
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    src = tmp_path / "in.mp4"
    _ffmpeg(["-f", "lavfi", "-i", "smptebars=size=320x180:rate=24", "-t", "10",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(src)])
    short = tmp_path / "short.wav"   # 3s 曲配 10s 片：老路径 wrap 两次硬接
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-af", "volume=-20dB", str(short)])

    out = tmp_path / "out.mp4"
    music_cfg = _music_cfg(music_start=0.5, fade_out=1.0,
                           loudnorm={"enabled": True, "i": -14.0, "tp": -1.5, "lra": 11.0})
    result = export_video(src, out, 0.0, 10.0, "",
                          {"resolution": "original", "fit": "pad", "quality": "low"},
                          short, music_cfg, False)
    assert out.exists()
    # 尾段（fade_out 之前）必须有声：拼贴份数不足会在这里露馅
    assert _tail_mean_volume(out, 7.5) > -50.0
    # 与 loudnorm 组合：测量遍与导出遍同链（都含拼贴），归一仍到位
    assert result["loudnorm"]["applied"] is True
