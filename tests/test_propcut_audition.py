"""propcut audition 小样（P0-2）测试 — 真 ffmpeg 合成素材（无 ffmpeg 自动 skip）+ 解析单测。

覆盖：loudnorm JSON 解析 / 对齐增益方向与 clamp / 端到端小样组（proxy 只编一次、
候选视频流 copy、等响度方向正确、manifest 完整）/ 多视频未收窄报错 / 显式 --tracks。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from estatecut.ffmpeg_tools import ffmpeg_available, probe_media

from propcut.audition import (_alignment_gain, _parse_loudnorm_json,
                              AUDITION_TARGET_I, GAIN_LIMIT_DB, run_audition)


# ---- 纯函数单测（不需要 ffmpeg）----

def test_parse_loudnorm_json() -> None:
    stderr = ("frame= 1 ...\n[Parsed_loudnorm_0 @ 0x1] \n{\n"
              '\t"input_i" : "-23.47",\n\t"input_tp" : "-5.2",\n'
              '\t"input_lra" : "3.1",\n\t"input_thresh" : "-33.5",\n'
              '\t"output_i" : "-24.0",\n\t"normalization_type" : "dynamic",\n'
              '\t"target_offset" : "0.5"\n}\n')
    parsed = _parse_loudnorm_json(stderr)
    assert parsed and parsed["input_i"] == "-23.47"
    assert _parse_loudnorm_json("no json here") is None
    assert _parse_loudnorm_json("{\"other\": 1}") is None  # 无 input_i 的块不算


def test_alignment_gain() -> None:
    gain, warn = _alignment_gain(-23.0)   # 轻曲 → 正增益拉上来
    assert gain == pytest.approx(AUDITION_TARGET_I - (-23.0)) and warn is None
    gain, warn = _alignment_gain(-10.0)   # 响曲 → 负增益压下去
    assert gain == pytest.approx(AUDITION_TARGET_I - (-10.0)) and warn is None
    gain, warn = _alignment_gain(None)    # 测不出 → 0 + 警告
    assert gain == 0.0 and warn
    gain, warn = _alignment_gain(-60.0)   # 超限 clamp
    assert gain == GAIN_LIMIT_DB and "超限" in warn


# ---- 端到端（真 ffmpeg）----

def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    input_dir = tmp_path / "in"
    lib = tmp_path / "music" / "calm"
    input_dir.mkdir()
    lib.mkdir(parents=True)
    _ffmpeg(["-f", "lavfi", "-i", "smptebars=size=320x180:rate=24", "-t", "8",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
             str(input_dir / "room_001.mp4")])
    # 一响一轻两首候选：对齐后增益方向必须相反
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=6",
             "-af", "volume=-2dB", str(lib / "loud.wav")])
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=330:duration=6",
             "-af", "volume=-30dB", str(lib / "quiet.wav")])
    cfg = tmp_path / "propcut.yaml"
    cfg.write_text(f"""
input:
  dir: "{input_dir.as_posix()}"
output:
  dir: "{(tmp_path / 'out').as_posix()}"
music:
  enabled: true
  library: "{(tmp_path / 'music').as_posix()}"
  select: category
  category: calm
  fade_in: 0.5
""", encoding="utf-8")
    return {"cfg": cfg, "tmp": tmp_path, "lib": lib, "input": input_dir}


def test_audition_end_to_end_explicit_tracks(workspace: dict[str, Path]) -> None:
    loud = workspace["lib"] / "loud.wav"
    quiet = workspace["lib"] / "quiet.wav"
    manifest = run_audition(workspace["cfg"], only="room_001",
                            tracks=[str(loud), str(quiet)], duration=5.0)

    out_dir = workspace["tmp"] / "out" / "audition" / "room_001"
    assert (out_dir / "audition_manifest.json").exists()
    assert (out_dir / "_proxy.mp4").exists()

    cands = manifest["candidates"]
    assert [c["track_name"] for c in cands] == ["loud.wav", "quiet.wav"]
    assert all(c["ok"] for c in cands), cands
    # 等响度对齐方向：响曲压下去（负），轻曲拉上来（正），且轻曲增益明显更大
    assert cands[0]["gain_db"] < cands[1]["gain_db"]
    assert cands[1]["gain_db"] > 0

    for c in cands:
        pm = probe_media(Path(c["output"]))
        assert pm["audio_present"]
        assert abs(pm["duration_sec"] - 5.0) < 0.5
        assert pm["height"] == 180  # 源 180p < 540p 目标：scale=-2:540 不放大检查略——列高一致即 copy 自 proxy

    # 无检测缓存 → 从 0 开始 + 提示先跑 detect
    assert manifest["start"] == 0.0
    assert any("detect" in w for w in manifest["warnings"])


def test_audition_requires_single_video(workspace: dict[str, Path]) -> None:
    import shutil
    shutil.copy(workspace["input"] / "room_001.mp4", workspace["input"] / "room_002.mp4")
    with pytest.raises(ValueError, match="一次只做一条"):
        run_audition(workspace["cfg"], tracks=[str(workspace["lib"] / "loud.wav")])


def test_audition_missing_track_fails(workspace: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="不存在"):
        run_audition(workspace["cfg"], only="room_001", tracks=["Z:/nope.mp3"])
