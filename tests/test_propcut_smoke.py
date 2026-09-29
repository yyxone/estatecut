"""propcut 端到端 smoke — 真 ffmpeg 合成"晃动开头→稳定房间"视频走完整管线。

音乐比视频短（3s sine），顺带验证循环补足；无 ffmpeg 环境自动 skip。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from estatecut.ffmpeg_tools import ffmpeg_available, probe_media

from propcut.pipeline import run

SHAKE_SEC = 4.0
TOTAL_SEC = 12.0


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


def _make_walkin_clip(path: Path) -> None:
    """前 4s 画面剧烈平移（模拟进门晃动），之后完全静止（模拟稳定房间展示）。"""
    expr_x = f"if(lt(t,{SHAKE_SEC}),160+140*sin(t*23),160)"
    expr_y = f"if(lt(t,{SHAKE_SEC}),90+80*cos(t*19),90)"
    _ffmpeg(["-f", "lavfi", "-i", "smptebars=size=640x360:rate=30",
             "-t", f"{TOTAL_SEC}", "-vf", f"crop=320:180:x='{expr_x}':y='{expr_y}'",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path)])


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    input_dir = tmp_path / "in"
    lib = tmp_path / "music" / "calm"
    input_dir.mkdir()
    lib.mkdir(parents=True)
    _make_walkin_clip(input_dir / "walkin_001.mp4")
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(lib / "tone.wav")])
    cfg_path = tmp_path / "propcut.yaml"
    cfg_path.write_text(f"""
input:
  dir: "{input_dir.as_posix()}"
output:
  dir: "{(tmp_path / 'out').as_posix()}"
detect:
  scan_seconds: 10
music:
  enabled: true
  library: "{(tmp_path / 'music').as_posix()}"
  select: category
  category: calm
  seed: 7
grading:
  preset: bright_interior
export:
  resolution: 640x360
  quality: low
""", encoding="utf-8")
    return {"cfg": cfg_path, "tmp": tmp_path, "input": input_dir}


def test_process_end_to_end(workspace: dict[str, Path]) -> None:
    results = run(workspace["cfg"], mode="process")
    assert len(results) == 1
    entry = results[0]
    assert entry["success"] and not entry["error"], entry
    assert entry["profile"] is None  # 无 profile 配置 → 导出路径条目仍带 profile 键（schema 一致）
    # 起点应落在晃动→稳定转折附近，而不是 0 或固定秒数
    assert 3.0 <= entry["detected_start_time"] <= 6.5, entry["detected_start_time"]
    assert entry["confidence"] >= 0.6
    assert entry["audio_mode"] == "remove"

    out = Path(entry["output"])
    assert out.exists()
    pm = probe_media(out)
    assert abs(pm["duration_sec"] - (TOTAL_SEC - entry["start_time_used"])) < 1.0
    assert pm["audio_present"]  # 3s 音乐循环补足到片长
    assert (pm["width"], pm["height"]) == (640, 360)

    reports = workspace["tmp"] / "out" / "_reports"
    assert (reports / "walkin_001.json").exists()
    for label in ("before", "selected", "after"):
        assert (reports / f"walkin_001_{label}.jpg").exists()
    assert list(reports.glob("run_*.jsonl"))


def test_detect_only_then_reprocess_with_override(workspace: dict[str, Path]) -> None:
    results = run(workspace["cfg"], mode="detect")
    assert results[0]["success"] and results[0]["output"] is None
    assert "profile" in results[0] and results[0]["profile"] is None  # detect-only 条目也带 profile 键

    # 人工覆盖起点 → reprocess 不重分析，按 override 导出
    (workspace["tmp"] / "overrides.json").write_text(
        json.dumps({"walkin_001.mp4": {"start_time": 2.0}}), encoding="utf-8")
    results = run(workspace["cfg"], mode="reprocess")
    entry = results[0]
    assert entry["success"] and entry["override_used"], entry
    assert entry["start_time_used"] == 2.0
    pm = probe_media(Path(entry["output"]))
    assert abs(pm["duration_sec"] - (TOTAL_SEC - 2.0)) < 1.0
    # override 关键帧另存，检测快照不被覆盖
    reports = workspace["tmp"] / "out" / "_reports"
    assert (reports / "walkin_001_override_selected.jpg").exists()
    assert (reports / "walkin_001_selected.jpg").exists()

    # reprocess 的语义 = 重新导出：改 override 后再跑必须真的重导，不被 overwrite=false 挡住
    (workspace["tmp"] / "overrides.json").write_text(
        json.dumps({"walkin_001.mp4": {"start_time": 3.0}}), encoding="utf-8")
    results = run(workspace["cfg"], mode="reprocess")
    entry = results[0]
    assert entry["success"] and not entry["skipped"], entry
    assert entry["start_time_used"] == 3.0
    pm = probe_media(Path(entry["output"]))
    assert abs(pm["duration_sec"] - (TOTAL_SEC - 3.0)) < 1.0

    # process 模式仍受 overwrite=false 保护：输出已存在 → skip
    results = run(workspace["cfg"], mode="process")
    assert results[0]["skipped"] and "已存在" in results[0]["error"]


def test_reprocess_rejects_stale_cache(workspace: dict[str, Path]) -> None:
    run(workspace["cfg"], mode="detect")
    # 改检测配置（scan_seconds 10→8）→ 缓存身份不匹配，reprocess 必须拒绝而不是静默用旧裁点
    stale_cfg = workspace["tmp"] / "propcut_changed.yaml"
    stale_cfg.write_text(
        workspace["cfg"].read_text(encoding="utf-8").replace("scan_seconds: 10", "scan_seconds: 8"),
        encoding="utf-8")
    results = run(stale_cfg, mode="reprocess")
    assert not results[0]["success"]
    assert "不匹配" in results[0]["error"]
    # 同样的不匹配走 process → 自动重测，正常出片
    results = run(stale_cfg, mode="process")
    assert results[0]["success"] and not results[0]["error"], results[0]


def test_reprocess_without_detection_fails(workspace: dict[str, Path]) -> None:
    results = run(workspace["cfg"], mode="reprocess")
    assert not results[0]["success"]
    assert "reprocess 需要已有检测结果" in results[0]["error"]


def test_process_appends_usage_ledger(workspace: dict[str, Path]) -> None:
    # 显式 usage_ledger → 导出成功后台账 append 一行，schema 完整（走完整 pipeline 验证 §台账）
    # 用绝对路径：相对路径会按 overrides.file 语义拼到 config_dir（本项目 tmp_path 是相对的）
    ledger = (workspace["tmp"] / "usage_ledger.jsonl").resolve()
    cfg_text = workspace["cfg"].read_text(encoding="utf-8").replace(
        "  category: calm\n", f'  category: calm\n  usage_ledger: "{ledger.as_posix()}"\n')
    cfg_path = workspace["tmp"] / "propcut_ledger.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")

    results = run(cfg_path, mode="process")
    entry = results[0]
    assert entry["success"] and not entry["error"], entry
    # 首跑台账为空 → 去重 normal，元信息进 run jsonl；favorites 默认关 → off（P1-1 接线）
    assert entry["music_dedup"]["mode"] == "normal"
    assert entry["music_dedup"]["favorites_mode"] == "off"

    assert ledger.exists()
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert set(r) == {"at", "run_id", "pipeline", "video", "source_video", "track_path",
                      "track_name", "category", "explicit", "pool", "license_scope",
                      "commercial_ok", "config"}
    assert re.fullmatch(r"[0-9a-f]{12}", r["run_id"])  # R5 L6：run 溯源标识
    assert r["pipeline"] == "propcut"
    assert r["source_video"] == "walkin_001.mp4"
    assert r["track_name"] == "tone.wav" and r["category"] == "calm"
    assert r["explicit"] is False
    assert r["video"].endswith("_cut.mp4")
    assert r["config"] == "propcut_ledger.yaml"

    # 选曲决策记录同步落盘，favorites_mode 进行式记录（P1-1 接线）
    sel_rows = [json.loads(x)
                for x in (ledger.parent / "selection_log.jsonl")
                .read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(sel_rows) == 1
    assert sel_rows[0]["favorites_mode"] == "off"
    assert sel_rows[0]["dedup_mode"] == "normal"


def test_keep_mode_skips_music_and_ledger(workspace: dict[str, Path]) -> None:
    # keep = 保留原声不加音乐 → 不选曲、不占去重窗、不写台账（export 本就丢弃 music_path）
    ledger = (workspace["tmp"] / "keep_ledger.jsonl").resolve()
    cfg_text = workspace["cfg"].read_text(encoding="utf-8").replace(
        "  category: calm\n",
        f'  category: calm\n  original_audio: keep\n  usage_ledger: "{ledger.as_posix()}"\n')
    cfg_path = workspace["tmp"] / "propcut_keep.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")

    entry = run(cfg_path, mode="process")[0]
    assert entry["success"] and not entry["error"], entry
    assert entry["audio_mode"] == "keep"
    assert entry["music"] is None                       # keep 不选曲
    assert entry["music_dedup"]["mode"] == "none"       # run jsonl 记 mode=none
    assert "ledger_write_warning" not in entry["music_dedup"]
    assert not ledger.exists()                          # 台账未创建 → 不占去重窗


def test_output_inside_input_rejected(workspace: dict[str, Path]) -> None:
    bad_cfg = workspace["tmp"] / "bad.yaml"
    bad_cfg.write_text(f"""
input:
  dir: "{workspace['input'].as_posix()}"
output:
  dir: "{(workspace['input'] / 'out').as_posix()}"
music:
  enabled: false
""", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        run(bad_cfg, mode="detect")
