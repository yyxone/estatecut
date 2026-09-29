"""propcut stitch 端到端 smoke — 真 ffmpeg 合成多段拼接走完整链路。

对抗点：三段源故意不同分辨率/帧率（段规格归一是 concat -c copy 的前提）；
音乐比成片短（3s sine，验证循环补足）；段级缓存复用与失效；配置错误 fail-fast。
无 ffmpeg 环境自动 skip。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from estatecut.ffmpeg_tools import ffmpeg_available, probe_media

from propcut.cli import main as cli_main
from propcut.config import ConfigError
from propcut.export import build_music_audio_chain
from propcut.stitch import run_stitch


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def _make_clip(path: Path, seconds: float, size: str, rate: int) -> None:
    _ffmpeg(["-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}",
             "-t", f"{seconds}", "-pix_fmt", "yuv420p",
             "-c:v", "libx264", "-preset", "ultrafast", str(path)])


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    input_dir = tmp_path / "in"
    lib = tmp_path / "music" / "calm"
    input_dir.mkdir()
    lib.mkdir(parents=True)
    # 三段故意不同分辨率/帧率：段规格归一后 concat 才不炸
    _make_clip(input_dir / "roomA.mp4", 3.0, "640x360", 30)
    _make_clip(input_dir / "roomB.mp4", 3.0, "320x240", 25)
    _make_clip(input_dir / "roomC.mp4", 3.0, "640x360", 30)
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(lib / "tone.wav")])

    ledger = (tmp_path / "usage_ledger.jsonl").resolve()
    cfg_path = tmp_path / "stitch.yaml"
    cfg_path.write_text(f"""
input:
  dir: "{input_dir.as_posix()}"
output:
  dir: "{(tmp_path / 'out').as_posix()}"
music:
  enabled: true
  library: "{(tmp_path / 'music').as_posix()}"
  select: category
  category: calm
  seed: 7
  fade_out: 1.0
  usage_ledger: "{ledger.as_posix()}"
grading:
  preset: bright_interior
export:
  resolution: original
  quality: low
stitch:
  output: "tour_test.mp4"
  clips:
    - {{file: "roomA.mp4", in: 0.5, out: 2.5}}
    - {{file: "roomB.mp4", out: 2.0}}
    - {{file: "roomC.mp4", in: 1.0}}
""", encoding="utf-8")
    return {"cfg": cfg_path, "tmp": tmp_path, "input": input_dir, "ledger": ledger}


def test_stitch_end_to_end(workspace: dict[str, Path]) -> None:
    entry = run_stitch(workspace["cfg"])
    assert entry["success"] and not entry["error"], entry

    # 统一段规格 = 首段有效尺寸/帧率（original 语义）
    assert entry["target"] == {"width": 640, "height": 360, "fps": 30.0}

    # 三段各 2s → 总长 ~6s；帧量化允许小漂移
    assert abs(entry["total_duration"] - 6.0) < 0.3, entry["total_duration"]
    out = Path(entry["output"])
    assert out.exists()
    pm = probe_media(out)
    assert abs(pm["duration_sec"] - entry["total_duration"]) < 0.5
    assert pm["audio_present"]  # 3s 音乐循环补足到 6s 成片
    assert (pm["width"], pm["height"]) == (640, 360)

    # 中间片必须无音轨（音频只在终混加入）；按 slug 分目录（多配置共用 output.dir 不踩缓存）
    work = workspace["tmp"] / "out" / "_stitch_work" / "tour_test"
    for i in range(3):
        seg = work / f"seg_{i:03d}.mp4"
        assert seg.exists()
        assert not probe_media(seg)["audio_present"]
    # 无声整片只服务终混；成品校验成功后必须清掉，避免与带声版一起长期占盘。
    assert not (work / "tour_test_silent.mp4").exists()

    # 报告：stitch json + 接缝抽帧（2 个拼接点 × 前后 2 帧）+ run jsonl
    reports = workspace["tmp"] / "out" / "_reports"
    assert (reports / "stitch_tour_test.json").exists()
    for i in (1, 2):
        for tag in ("a", "b"):
            assert (reports / f"stitch_tour_test_seam{i:02d}_{tag}.jpg").exists()
    assert list(reports.glob("run_*.jsonl"))

    # 台账 append 一行：video/source_video 都是成片名（一条成片 = 去重窗一格）
    rows = [json.loads(x) for x in workspace["ledger"].read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 1
    assert rows[0]["video"] == "tour_test.mp4"
    assert rows[0]["source_video"] == "tour_test.mp4"
    assert rows[0]["pipeline"] == "propcut"
    assert rows[0]["config"] == "stitch.yaml"


def test_stitch_segment_cache_reuse_and_invalidate(workspace: dict[str, Path]) -> None:
    first = run_stitch(workspace["cfg"])
    assert first["success"], first
    assert all(not c["reused_cache"] for c in first["clips"])

    # 原样重跑 → 三段全部走缓存
    second = run_stitch(workspace["cfg"])
    assert second["success"], second
    assert all(c["reused_cache"] for c in second["clips"])

    # 只改第 0 段剪点 → 仅该段重编码，其余复用
    changed = workspace["tmp"] / "stitch_changed.yaml"
    changed.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace("in: 0.5", "in: 0.8"),
        encoding="utf-8")
    third = run_stitch(changed)
    assert third["success"], third
    assert [c["reused_cache"] for c in third["clips"]] == [False, True, True]


def test_stitch_cli_dispatch(workspace: dict[str, Path]) -> None:
    assert cli_main(["stitch", "--config", str(workspace["cfg"])]) == 0


def test_stitch_music_disabled_outputs_silent(workspace: dict[str, Path]) -> None:
    cfg = workspace["tmp"] / "stitch_nomusic.yaml"
    cfg.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
        encoding="utf-8")
    entry = run_stitch(cfg)
    assert entry["success"], entry
    assert entry["music"] is None
    assert not probe_media(Path(entry["output"]))["audio_present"]


def test_stitch_rejects_keep_mix(workspace: dict[str, Path]) -> None:
    cfg = workspace["tmp"] / "stitch_keep.yaml"
    cfg.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace(
            "  select: category\n", "  select: category\n  original_audio: keep\n"),
        encoding="utf-8")
    with pytest.raises(ConfigError, match="original_audio=remove"):
        run_stitch(cfg)


def test_stitch_missing_file_and_bad_edl_failfast(workspace: dict[str, Path]) -> None:
    cfg = workspace["tmp"] / "stitch_missing.yaml"
    cfg.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace("roomB.mp4", "ghost.mp4"),
        encoding="utf-8")
    with pytest.raises(ConfigError, match="不存在"):
        run_stitch(cfg)

    # in >= 源时长（out 钳到时长后 in<out 不成立）→ fail-fast 不出半截片
    cfg2 = workspace["tmp"] / "stitch_badcut.yaml"
    cfg2.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace("in: 1.0", "in: 9.0"),
        encoding="utf-8")
    with pytest.raises(ConfigError, match="剪点无效"):
        run_stitch(cfg2)


def test_stitch_config_validation_at_load(workspace: dict[str, Path]) -> None:
    # output 带路径分隔符 → load_config 阶段 fail-fast
    cfg = workspace["tmp"] / "stitch_badout.yaml"
    cfg.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace(
            'output: "tour_test.mp4"', 'output: "sub/tour.mp4"'),
        encoding="utf-8")
    with pytest.raises(ConfigError, match="stitch.output"):
        run_stitch(cfg)

    # in > out → load_config 阶段 fail-fast
    cfg2 = workspace["tmp"] / "stitch_inout.yaml"
    cfg2.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace("in: 0.5, out: 2.5", "in: 2.5, out: 0.5"),
        encoding="utf-8")
    with pytest.raises(ConfigError, match="剪点无效"):
        run_stitch(cfg2)


def test_stitch_clip_escape_rejected(tmp_path: Path) -> None:
    # R1 S1：clips[].file 经 ../ 或绝对路径 resolve 后逃出 input.dir → ConfigError，
    # 且 containment 检查必须先于 probe（无 ffmpeg 环境也强制执行，本测试不 skip）。
    from propcut.stitch import _resolve_clips

    input_dir = tmp_path / "in"
    input_dir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"\x00" * 128)

    with pytest.raises(ConfigError, match="input.dir"):
        _resolve_clips({"clips": [{"file": "../outside.mp4"}]}, input_dir)
    with pytest.raises(ConfigError, match="input.dir"):
        _resolve_clips({"clips": [{"file": str(outside.resolve())}]}, input_dir)


def test_stitch_final_verify_fail_preserves_existing_output(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # R1 E1（stitch 面）：终混 .part 校验必须发生在 replace 之前——
    # 坏 .part（时长离谱）绝不覆盖已存在的旧成片。
    import propcut.stitch as stitch_mod

    first = run_stitch(workspace["cfg"])
    assert first["success"], first
    out = Path(first["output"])
    good_bytes = out.read_bytes()

    real_probe = stitch_mod.probe_media

    def _tampered(path):
        info = dict(real_probe(path))
        if ".part" in Path(path).name:
            info["duration_sec"] = 999.0  # 模拟坏 .part
        return info

    monkeypatch.setattr(stitch_mod, "probe_media", _tampered)
    second = run_stitch(workspace["cfg"])
    assert not second["success"]
    assert "校验" in (second["error"] or ""), second
    assert out.read_bytes() == good_bytes  # 旧成片一字未动


def test_build_music_audio_chain_shapes() -> None:
    cfg = {"volume": 0.75, "fade_in": 1.5, "fade_out": 2.5, "music_start": 0.0, "original_volume": 1.0}
    chain = build_music_audio_chain(cfg, 60.0)
    assert chain.startswith("[1:a]volume=0.75")
    assert "afade=t=in:st=0:d=1.500" in chain
    assert "afade=t=out:st=57.500:d=2.500" in chain
    assert chain.endswith("[outa]")
    assert ";" not in chain  # remove 模式单链

    # 短片：淡入淡出钳到 target/2
    short = build_music_audio_chain(cfg, 2.0)
    assert "afade=t=in:st=0:d=1.000" in short
    assert "afade=t=out:st=1.000:d=1.000" in short

    # mix 模式：原声 + amix 三段图
    mixed = build_music_audio_chain({**cfg, "original_volume": 0.5}, 60.0, mix_original=True)
    assert mixed.startswith("[0:a]volume=0.5[a0];[1:a]")
    assert "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[outa]" in mixed

    # music_start 走 atrim + asetpts
    with_start = build_music_audio_chain({**cfg, "music_start": 3.0}, 60.0)
    assert "atrim=start=3.000,asetpts=PTS-STARTPTS" in with_start
