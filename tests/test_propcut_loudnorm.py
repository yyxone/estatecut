"""propcut 两遍 loudnorm 响度归一（P0-3）测试。

覆盖：config 校验（默认关/未知键/范围）/ loudnorm_filter 拼装 / resolve_audio_mode
降级链 / 静音输入 -inf 降级（真 ffmpeg）/ build_export_command 接线 /
端到端：极轻曲开归一 → 成品 integrated ≈ 目标 I；关归一 → 明显偏离（P0-0 病根复现）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from estatecut.ffmpeg_tools import ffmpeg_available

from propcut.config import ConfigError, load_config
from propcut.export import (build_export_command, build_measure_command, export_video,
                            loudnorm_filter, measure_timeline_loudness, resolve_audio_mode)

MEASURED = {"input_i": "-30.20", "input_tp": "-8.10", "input_lra": "2.5",
            "input_thresh": "-40.5", "target_offset": "0.3"}


# ---- 纯函数 ----

def test_loudnorm_filter_two_passes() -> None:
    ln = {"i": -14.0, "tp": -1.5, "lra": 11.0}
    first = loudnorm_filter(ln)
    assert first == "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json"
    second = loudnorm_filter(ln, MEASURED)
    assert "measured_I=-30.20" in second and "measured_thresh=-40.5" in second
    assert "offset=0.3" in second and "linear=true" in second
    assert second.endswith(",aresample=48000")  # loudnorm 内部 192k，必须归回 48k
    assert "print_format" not in second


def test_resolve_audio_mode_branches() -> None:
    music = Path("m.mp3")
    assert resolve_audio_mode({"original_audio": "remove"}, None, True) == ("remove_silent", None)
    assert resolve_audio_mode({"original_audio": "mix"}, None, True) == ("keep", None)
    assert resolve_audio_mode({"original_audio": "mix"}, music, False) == ("remove", music)
    assert resolve_audio_mode({"original_audio": "keep"}, music, True) == ("keep", None)
    assert resolve_audio_mode({"original_audio": "remove"}, music, True) == ("remove", music)


def _music_cfg(**loudnorm) -> dict:
    return {"original_audio": "remove", "volume": 0.8, "fade_in": 0.0, "fade_out": 2.0,
            "music_start": 0.0,
            "loudnorm": {"enabled": True, "i": -14.0, "tp": -1.5, "lra": 11.0, **loudnorm}}


def test_build_export_command_wires_loudnorm(tmp_path: Path) -> None:
    args = (tmp_path / "in.mp4", tmp_path / "out.mp4", 0.0, 10.0, "", {"resolution": "original", "fit": "pad", "quality": "high"},
            tmp_path / "m.mp3", _music_cfg(), False)
    build_argv, _, _ = build_export_command(*args, loudnorm_measured=MEASURED)
    fc = build_argv([])[build_argv([]).index("-filter_complex") + 1]
    assert "[premix]" in fc and "linear=true" in fc and "aresample=48000" in fc
    # 不传 measured（测量失败降级 / 未启用）→ 音频链保持现状
    build_argv2, _, _ = build_export_command(*args, loudnorm_measured=None)
    fc2 = build_argv2([])[build_argv2([]).index("-filter_complex") + 1]
    assert "loudnorm" not in fc2 and "[premix]" not in fc2


# ---- config ----

def _write_cfg(tmp_path: Path, extra: str) -> Path:
    (tmp_path / "in").mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = tmp_path / "p.yaml"
    cfg.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{lib.as_posix()}"\n{extra}',
        encoding="utf-8")
    return cfg


def test_config_loudnorm_default_off(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path, ""))
    assert cfg["music"]["loudnorm"] == {"enabled": False, "i": -14.0, "tp": -1.5, "lra": 11.0}


def test_config_loudnorm_partial_merge_and_errors(tmp_path: Path) -> None:
    cfg = load_config(_write_cfg(tmp_path, "  loudnorm:\n    enabled: true\n"))
    assert cfg["music"]["loudnorm"]["enabled"] is True
    assert cfg["music"]["loudnorm"]["i"] == -14.0  # 部分给键 → 其余补默认
    with pytest.raises(ConfigError, match="未知键"):
        load_config(_write_cfg(tmp_path, "  loudnorm:\n    target: -14\n"))
    with pytest.raises(ConfigError, match="loudnorm.i"):
        load_config(_write_cfg(tmp_path, "  loudnorm:\n    i: -80\n"))
    with pytest.raises(ConfigError, match="enabled"):
        load_config(_write_cfg(tmp_path, "  loudnorm:\n    enabled: 1\n"))


# ---- 真 ffmpeg ----

def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def _integrated_lufs(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                           "-af", "ebur128", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", shell=False)
    m = re.search(r"I:\s+(-?[\d.]+)\s+LUFS", proc.stderr.split("Summary:")[-1])
    assert m, proc.stderr[-800:]
    return float(m.group(1))


@pytest.fixture()
def media(tmp_path: Path) -> dict[str, Path]:
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    src = tmp_path / "in.mp4"
    _ffmpeg(["-f", "lavfi", "-i", "smptebars=size=320x180:rate=24", "-t", "8",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(src)])
    quiet = tmp_path / "quiet.wav"   # 极轻曲：P0-0 病根（配乐响度不受控）的复现素材
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=330:duration=6",
             "-af", "volume=-30dB", str(quiet)])
    silent = tmp_path / "silent.wav"
    _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=6", str(silent)])
    return {"src": src, "quiet": quiet, "silent": silent, "tmp": tmp_path}


def test_measure_silent_track_degrades(media: dict[str, Path]) -> None:
    """全静音曲 → input_i=-inf → 降级不归一 + warning（linear 模式硬塞 -inf 会炸）。"""
    argv = build_measure_command(media["src"], 0.0, 5.0, media["silent"], _music_cfg(), False)
    measured, warn = measure_timeline_loudness(argv)
    assert measured is None
    assert warn and "不做响度归一" in warn


def test_pipeline_passes_loudnorm_report(media: dict[str, Path]) -> None:
    """process 全管线：export_video 的 loudnorm 报告要透传进 run 报告条目。"""
    from propcut.pipeline import run

    tmp = media["tmp"]
    input_dir = tmp / "in_pipe"
    lib = tmp / "music" / "calm"
    input_dir.mkdir()
    lib.mkdir(parents=True, exist_ok=True)
    # 前 3s 画面平移（进门晃动）→ 之后静止：让起点检测有戏可唱
    _ffmpeg(["-f", "lavfi", "-i", "smptebars=size=640x360:rate=30", "-t", "10",
             "-vf", "crop=320:180:x='if(lt(t,3),160+140*sin(t*23),160)'"
                    ":y='if(lt(t,3),90+80*cos(t*19),90)'",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
             str(input_dir / "walkin.mp4")])
    _ffmpeg(["-i", str(media["quiet"]), "-c", "copy", str(lib / "quiet.wav")])

    cfg = tmp / "pipe.yaml"
    cfg.write_text(f"""
input:
  dir: "{input_dir.as_posix()}"
output:
  dir: "{(tmp / 'out_pipe').as_posix()}"
detect:
  scan_seconds: 8
music:
  enabled: true
  library: "{(tmp / 'music').as_posix()}"
  select: category
  category: calm
  seed: 7
  loudnorm:
    enabled: true
export:
  resolution: original
  quality: low
""", encoding="utf-8")
    entry = run(cfg, mode="process")[0]
    assert entry["success"] and not entry["error"], entry
    assert entry["loudnorm"]["applied"] is True
    assert entry["loudnorm"]["target_i"] == -14.0
    i_out = _integrated_lufs(Path(entry["output"]))
    assert abs(i_out - (-14.0)) < 1.0, f"pipeline={i_out}"


def test_stitch_end_to_end_normalizes(media: dict[str, Path]) -> None:
    from propcut.stitch import run_stitch

    tmp = media["tmp"]
    input_dir = tmp / "in_stitch"
    lib = tmp / "music" / "calm"
    input_dir.mkdir()
    lib.mkdir(parents=True)
    _ffmpeg(["-i", str(media["src"]), "-t", "3", "-c", "copy", str(input_dir / "a.mp4")])
    _ffmpeg(["-i", str(media["src"]), "-t", "3", "-c", "copy", str(input_dir / "b.mp4")])
    _ffmpeg(["-i", str(media["quiet"]), "-c", "copy", str(lib / "quiet.wav")])

    cfg = tmp / "stitch.yaml"
    cfg.write_text(f"""
input:
  dir: "{input_dir.as_posix()}"
output:
  dir: "{(tmp / 'out_stitch').as_posix()}"
music:
  enabled: true
  library: "{(tmp / 'music').as_posix()}"
  select: category
  category: calm
  seed: 7
  usage_ledger: "off"
  loudnorm:
    enabled: true
export:
  resolution: original
  quality: low
stitch:
  output: "tour.mp4"
  clips:
    - {{file: "a.mp4"}}
    - {{file: "b.mp4"}}
""", encoding="utf-8")
    entry = run_stitch(cfg)
    assert entry["success"], entry
    assert entry["loudnorm"]["applied"] is True
    assert entry["loudnorm"]["measured_i"] < -25
    i_out = _integrated_lufs(Path(entry["output"]))
    assert abs(i_out - (-14.0)) < 1.0, f"stitch={i_out}"


def test_export_end_to_end_normalizes(media: dict[str, Path]) -> None:
    export_cfg = {"resolution": "original", "fit": "pad", "quality": "low"}

    out_off = media["tmp"] / "off.mp4"
    mus_off = _music_cfg(enabled=False)
    r_off = export_video(media["src"], out_off, 0.0, 8.0, "", export_cfg,
                         media["quiet"], mus_off, False)
    assert "loudnorm" not in r_off
    i_off = _integrated_lufs(out_off)

    out_on = media["tmp"] / "on.mp4"
    r_on = export_video(media["src"], out_on, 0.0, 8.0, "", export_cfg,
                        media["quiet"], _music_cfg(), False)
    assert r_on["loudnorm"]["applied"] is True
    assert r_on["loudnorm"]["measured_i"] < -25  # 极轻曲测出来就该很低
    i_on = _integrated_lufs(out_on)

    # 病根复现：不归一的成片跟着曲子响度走（远低于 -14）；归一后落在目标 ±1 LU
    assert i_off < -25, f"off={i_off}"
    assert abs(i_on - (-14.0)) < 1.0, f"on={i_on}"
