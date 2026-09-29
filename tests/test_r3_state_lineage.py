"""R3 state/人审锁/产物 lineage 回归 — 批次卡 docs/reviews/repair-2026-07/R3.md 测试计划 T1-T13。

- talkcut 部分（T1-T9/T13）纯逻辑，无 ffmpeg 依赖；
- propcut 行为级用例（T11 后半 / T12）需 ffmpeg，无则 skip（与既有 propcut 测试同约定）；
- propcut.utils / stitch._seg_identity 在函数内 import（TEST_RED 阶段尚不存在，
  模块级 import 会炸掉整个文件的收集）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from estatecut.ffmpeg_tools import ffmpeg_available

from talkcut import STAGES
from talkcut.state import (LOCKS, load_state, mark_stage, require_lock,
                           save_state, set_lock, state_path)

# ---------------------------------------------------------------- talkcut 助手


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "o"
    out.mkdir(exist_ok=True)
    return out


def _write_transcript(out: Path) -> Path:
    p = out / "transcript.json"
    p.write_text(json.dumps({
        "source": {"path": "source.mp4", "duration_sec": 6.0},
        "segments": [{"start": 0.0, "end": 3.0, "text": "今天看房", "words": []}],
        "gap_audit": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _write_edl(out: Path, human_locked: bool = True) -> Path:
    p = out / "edl.json"
    p.write_text(json.dumps({
        "project": "p", "source_timeline": "source.mp4", "human_locked": human_locked,
        "keep_ranges": [{"src_start": 0.0, "src_end": 2.5, "label": "段1"},
                        {"src_start": 3.5, "src_end": 5.5, "label": "段2"}],
        "cuts": [], "splices": [], "broll_cover": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _write_srt(out: Path) -> Path:
    p = out / "字幕.srt"
    p.write_text("1\n00:00:00,000 --> 00:00:02,000\n今天看房\n\n", encoding="utf-8")
    return p


def _default_state_dict() -> dict:
    return {
        "project": "p",
        "stages": {s: {"status": "pending", "outputs": [], "verified": False} for s in STAGES},
        "locks": {k: False for k in LOCKS},
    }


def _prime_all_done_and_locked(out: Path) -> None:
    """全 7 阶段 done + 3 锁上锁（产物先落盘，锁在最后上——中途 done 会重置锁）。"""
    _write_transcript(out)
    _write_edl(out)
    _write_srt(out)
    for s in STAGES:
        mark_stage(out, "p", s, "done", [], True)
    for lk in LOCKS:
        set_lock(out, "p", lk, True)


# ------------------------------------------------------- T1/T2/T3/T4/T13（L1）


def test_t1_set_lock_writes_approval_record(tmp_path):
    out = _out(tmp_path)
    _write_transcript(out)
    set_lock(out, "p", "transcript_lock", True)
    st = json.loads(state_path(out).read_text(encoding="utf-8"))
    rec = st["locks"]["transcript_lock"]
    assert isinstance(rec, dict), f"锁必须是绑定产物的审批记录而非布尔，实际 {rec!r}"
    assert rec["artifact"] == "transcript.json"
    assert isinstance(rec["sha256"], str) and len(rec["sha256"]) == 64
    assert rec["approved_at"]
    assert rec.get("approved_by", "human") == "human"
    require_lock(out, "p", "transcript_lock")  # 记录锁 + 产物未改 → 放行


def test_t2_legacy_bool_true_fail_closed(tmp_path):
    """旧 state 里的 true 无法证明批的是哪份产物 → require_lock 拒绝并提示重新上锁。"""
    out = _out(tmp_path)
    _write_edl(out)
    d = _default_state_dict()
    d["locks"]["cut_lock"] = True
    state_path(out).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="重新"):
        require_lock(out, "p", "cut_lock")


@pytest.mark.parametrize("lock, writer", [
    ("transcript_lock", _write_transcript),
    ("subtitle_lock", _write_srt),
])
def test_t3_whole_file_artifact_tamper_breaks_lock(tmp_path, lock, writer):
    out = _out(tmp_path)
    artifact = writer(out)
    set_lock(out, "p", lock, True)
    with artifact.open("a", encoding="utf-8") as h:  # 改 1 字节（追加）
        h.write("x")
    with pytest.raises(RuntimeError, match="被改"):
        require_lock(out, "p", lock)


def test_t3_edl_decision_surface_tamper_breaks_lock(tmp_path):
    out = _out(tmp_path)
    _write_edl(out)
    set_lock(out, "p", "cut_lock", True)
    edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
    edl["keep_ranges"][0]["src_end"] = 2.0  # 人审后偷偷改剪点
    (out / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(RuntimeError, match="被改"):
        require_lock(out, "p", "cut_lock")


def test_t4_out_start_backfill_does_not_break_cut_lock(tmp_path):
    """rough_cut 成功后回填 out_start（cut.py 机器派生字段）不属人审决策面 → 不破锁。"""
    out = _out(tmp_path)
    _write_edl(out)
    set_lock(out, "p", "cut_lock", True)
    edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
    acc = 0.0
    for kr in edl["keep_ranges"]:  # 同 cut.py 回填逻辑
        kr["out_start"] = round(acc, 3)
        acc += float(kr["src_end"]) - float(kr["src_start"])
    (out / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    require_lock(out, "p", "cut_lock")  # 不应 raise


def test_t13_cmd_lock_hash_covers_final_content(tmp_path):
    """cmd_lock(cut_lock) 顺序修正：先回写 edl.human_locked 再上锁，hash 盖最终内容。"""
    from talkcut import cli
    out = _out(tmp_path)
    _write_edl(out, human_locked=False)
    cli.main(["lock", "cut_lock", "--out", str(out), "--project", "p"])
    st = json.loads(state_path(out).read_text(encoding="utf-8"))
    assert isinstance(st["locks"]["cut_lock"], dict), "cmd_lock 后应为审批记录"
    edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
    assert edl["human_locked"] is True
    require_lock(out, "p", "cut_lock")  # 上锁后立即校验必须放行


# ------------------------------------------------------------- T5/T6（L4 DAG）


def test_t5_transcribe_redone_invalidates_downstream_and_locks(tmp_path):
    out = _out(tmp_path)
    _prime_all_done_and_locked(out)
    mark_stage(out, "p", "transcribe", "done", [], True)  # 上游重跑
    st = load_state(out, "p")
    for s in ("paper_edit", "rough_cut", "fine_cut", "subtitle", "qa_export"):
        assert st["stages"][s]["status"] == "pending", f"{s} 应被失效为 pending"
        assert "transcribe" in st["stages"][s].get("note", ""), f"{s} note 应说明被谁失效"
    assert st["stages"]["ingest"]["status"] == "done"
    assert st["stages"]["transcribe"]["status"] == "done"
    for lk in LOCKS:
        assert st["locks"][lk] is False, f"{lk} 应被重置为 False"


def test_t6_rough_cut_redone_keeps_subtitle_and_its_lock(tmp_path):
    """DAG 非线性：subtitle 依赖 transcript+edl，不依赖 rough_cut 成片 → 不被失效。"""
    out = _out(tmp_path)
    _prime_all_done_and_locked(out)
    mark_stage(out, "p", "rough_cut", "done", [], True)  # rough_cut 重跑
    st = load_state(out, "p")
    for s in ("fine_cut", "qa_export"):
        assert st["stages"][s]["status"] == "pending", f"{s} 应被失效为 pending"
    assert st["stages"]["subtitle"]["status"] == "done"
    assert st["stages"]["paper_edit"]["status"] == "done"
    assert isinstance(st["locks"]["subtitle_lock"], dict), "subtitle_lock 不应被 rough_cut 重跑重置"
    assert isinstance(st["locks"]["cut_lock"], dict)
    assert isinstance(st["locks"]["transcript_lock"], dict)


# ---------------------------------------------------------------- T7（L2 原子写）


def test_t7_interrupted_replace_preserves_state(tmp_path, monkeypatch):
    out = _out(tmp_path)
    mark_stage(out, "p", "ingest", "done", ["source.mp4"], True)
    original = state_path(out).read_text(encoding="utf-8")
    st = load_state(out, "p")
    st["stages"]["transcribe"]["status"] = "done"

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_state(out, st)
    monkeypatch.undo()
    assert state_path(out).read_text(encoding="utf-8") == original, "中断后原 state.json 必须完好"
    assert not list(out.glob("*.tmp")), "中断后不留 .tmp 残骸"


# ---------------------------------------------------------------- T8（L3 显式校验）


def _drop_stage(d: dict) -> dict:
    del d["stages"]["qa_export"]
    return d


def _bad_status(d: dict) -> dict:
    d["stages"]["rough_cut"]["status"] = "finished"
    return d


def _bad_lock_type(d: dict) -> dict:
    d["locks"]["cut_lock"] = "yes"
    return d


@pytest.mark.parametrize("mutate, needle", [
    (_drop_stage, "qa_export"),
    (_bad_status, "status"),
    (_bad_lock_type, "cut_lock"),
])
def test_t8_malformed_state_explicit_error(tmp_path, mutate, needle):
    out = _out(tmp_path)
    d = mutate(_default_state_dict())
    state_path(out).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_state(out, "p")
    msg = str(ei.value)
    assert "state.json" in msg, f"错误须带路径：{msg}"
    assert needle in msg, f"错误须指明字段 {needle}：{msg}"


def test_t8_non_json_state_explicit_error(tmp_path):
    out = _out(tmp_path)
    state_path(out).write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_state(out, "p")
    assert "state.json" in str(ei.value), "JSONDecodeError 须包一层带 state.json 路径"


# ---------------------------------------------------------------- T9（L6 陈旧产物门）


def test_t9_stale_fine_cut_blocks_export(tmp_path):
    from talkcut.qaexport import _src_for_export
    out = _out(tmp_path)
    fine = out / "fine_cut.mp4"
    fine.write_bytes(b"stale bytes")
    st = _default_state_dict()  # fine_cut = pending：文件是上一轮陈旧产物
    with pytest.raises(RuntimeError, match="fine_cut"):
        _src_for_export(out, st)
    # 本轮 done（mark_stage 记录产物指纹）→ 放行
    st2 = mark_stage(out, "p", "fine_cut", "done", [str(fine)], True)
    assert _src_for_export(out, st2).name == "fine_cut.mp4"


# ---------------------------------------------------------------- T10（C1 检测缓存身份）


def test_t10_cache_matches_source_identity(tmp_path):
    from propcut.pipeline import ANALYZER_VERSION, DETECT_CFG_KEYS, _cache_matches
    from propcut.utils import src_identity

    src = tmp_path / "v.mp4"
    src.write_bytes(b"A" * 4096)
    detect_cfg = {"scan_seconds": 10, "sample_fps": 2, "stable_window": 1.0, "max_trim_seconds": 20}
    cached = {
        "video": str(src),
        "probe": {"duration_sec": 10.0},
        "detect_cfg": {k: detect_cfg.get(k) for k in DETECT_CFG_KEYS},
        "src_identity": src_identity(src),
        "analyzer_version": ANALYZER_VERSION,
    }
    assert _cache_matches(cached, src, 10.0, detect_cfg) is True  # 完全一致 → 复用

    legacy = {k: v for k, v in cached.items() if k != "src_identity"}
    assert _cache_matches(legacy, src, 10.0, detect_cfg) is False  # legacy 缓存 → 重测

    # 同长替换（重拍同机位）：内容变、size 不变、mtime 复原 → 只剩 quick_hash 能兜住
    stat = src.stat()
    src.write_bytes(b"B" * 4096)
    os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _cache_matches(cached, src, 10.0, detect_cfg) is False


# ---------------------------------------------------------------- T11（C2 stitch 段缓存）


def test_t11_seg_identity_shape(tmp_path):
    """段缓存身份纯逻辑：src_identity 三字段，不再有秒级浮点 mtime。"""
    from propcut.stitch import _seg_identity

    src = tmp_path / "c.mp4"
    src.write_bytes(b"C" * 4096)
    clip = {"src": src, "in": 0.0, "out": 1.5}
    ident = _seg_identity(clip, "scale=320:180", "low", True)
    assert set(ident["src_identity"]) == {"size", "mtime_ns", "quick_hash", "algo"}
    assert "mtime" not in ident
    assert ident["src"] == str(src) and ident["in"] == 0.0 and ident["out"] == 1.5
    assert ident["vchain"] == "scale=320:180" and ident["quality"] == "low"
    assert ident["color_tag"] is True


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, shell=False)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture()
def stitch_ws(tmp_path: Path) -> dict[str, Path]:
    if not ffmpeg_available():
        pytest.skip("FFmpeg/ffprobe is not available")
    input_dir = tmp_path / "in"
    lib = tmp_path / "music" / "calm"
    input_dir.mkdir()
    lib.mkdir(parents=True)
    _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", "2",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
             str(input_dir / "a.mp4")])
    _ffmpeg(["-f", "lavfi", "-i", "smptebars=size=320x180:rate=30", "-t", "2",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
             str(input_dir / "b.mp4")])
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(lib / "tone.wav")])
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
export:
  resolution: 320x180
  quality: low
stitch:
  output: stitched.mp4
  clips:
    - file: a.mp4
    - file: b.mp4
      in: 0.5
      out: 1.5
""", encoding="utf-8")
    return {"cfg": cfg_path, "tmp": tmp_path}


def test_t11_stitch_sidecar_src_identity_and_legacy_not_reused(stitch_ws):
    from propcut.stitch import run_stitch

    entry = run_stitch(stitch_ws["cfg"])
    assert entry["success"], entry
    sidecar = stitch_ws["tmp"] / "out" / "_stitch_work" / "stitched" / "seg_000.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert set(data.get("src_identity") or {}) == {"size", "mtime_ns", "quick_hash", "algo"}, data
    assert "mtime" not in data, "秒级浮点 st_mtime 身份已废弃（可撞同值）"

    entry2 = run_stitch(stitch_ws["cfg"])
    assert [c["reused_cache"] for c in entry2["clips"]] == [True, True]

    # 伪造 legacy sidecar（mtime 浮点、无 src_identity）→ 该段不复用，一次性重编码
    legacy = {k: v for k, v in data.items() if k != "src_identity"}
    legacy["mtime"] = Path(data["src"]).stat().st_mtime
    sidecar.write_text(json.dumps(legacy), encoding="utf-8")
    entry3 = run_stitch(stitch_ws["cfg"])
    assert [c["reused_cache"] for c in entry3["clips"]] == [False, True]


# ---------------------------------------------------------------- T12（C3 输出身份 sidecar）

SHAKE_SEC = 4.0
TOTAL_SEC = 12.0


def _make_walkin_clip(path: Path) -> None:
    """同 test_propcut_smoke：前 4s 剧烈平移（进门晃动），之后静止（稳定房间）。"""
    expr_x = f"if(lt(t,{SHAKE_SEC}),160+140*sin(t*23),160)"
    expr_y = f"if(lt(t,{SHAKE_SEC}),90+80*cos(t*19),90)"
    _ffmpeg(["-f", "lavfi", "-i", "smptebars=size=640x360:rate=30",
             "-t", f"{TOTAL_SEC}", "-vf", f"crop=320:180:x='{expr_x}':y='{expr_y}'",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path)])


@pytest.fixture()
def pipeline_ws(tmp_path: Path) -> dict[str, Path]:
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
    return {"cfg": cfg_path, "tmp": tmp_path}


def test_t12_output_sidecar_written_and_intent_mismatch_errors(pipeline_ws):
    from propcut.pipeline import run

    # 1) 导出成功 → 写输出身份 sidecar，意图面 v2 齐全（分辨率/fit/音乐面/调色链）
    entry = run(pipeline_ws["cfg"], mode="process")[0]
    assert entry["success"] and not entry["error"], entry
    sc_path = pipeline_ws["tmp"] / "out" / "_reports" / "walkin_001.output.json"
    assert sc_path.exists(), "导出成功必须写 _reports/<slug>.output.json"
    sc = json.loads(sc_path.read_text(encoding="utf-8"))
    assert {"intent_version", "src_identity", "start_time_used", "grading", "quality",
            "resolution", "fit", "music", "out_identity", "created"} <= set(sc)
    assert sc["intent_version"] >= 2
    assert set(sc["src_identity"]) == {"size", "mtime_ns", "quick_hash", "algo"}
    assert {"mode", "preset", "hdr_tonemapped", "filter"} <= set(sc["grading"])
    assert {"original_audio", "volume", "fade_in", "fade_out"} <= set(sc["music"])

    # 2) 意图一致 → overwrite=false 照旧跳过（skipped-success 语义保留）
    entry2 = run(pipeline_ws["cfg"], mode="process")[0]
    assert entry2["skipped"] and entry2["success"], entry2

    # 3) legacy 输出（无 sidecar）→ fail-closed error（round-1 的 skipped+warning 被 Codex F7 推翻：
    #    warning 不改自动化成功语义，等于替用户确认了来历不明的旧片）
    bak = sc_path.with_suffix(".bak")
    sc_path.rename(bak)
    entry3 = run(pipeline_ws["cfg"], mode="process")[0]
    assert not entry3["success"] and not entry3["skipped"], entry3
    assert "sidecar" in (entry3["error"] or ""), entry3
    bak.rename(sc_path)

    # 4) 输出被同名换片（sidecar 未动、换成另一个健康 mp4）→ out_identity 不符 → error（F7）
    out_file = pipeline_ws["tmp"] / "out" / "walkin_001_cut.mp4"
    other = pipeline_ws["tmp"] / "other.mp4"
    _ffmpeg(["-f", "lavfi", "-i", "color=red:size=640x360:rate=30", "-t", "2",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(other)])
    stat0 = out_file.stat()
    orig_bytes = out_file.read_bytes()
    out_file.write_bytes(other.read_bytes())
    entry4 = run(pipeline_ws["cfg"], mode="process")[0]
    assert not entry4["success"] and not entry4["skipped"], entry4
    assert "指纹" in (entry4["error"] or ""), entry4
    out_file.write_bytes(orig_bytes)
    os.utime(out_file, ns=(stat0.st_atime_ns, stat0.st_mtime_ns))  # 复原身份（含 mtime_ns）

    # 5) 配置意图变了（分辨率）→ error（F6：round-1 意图面漏掉 resolution/fit/音乐面）
    cfg2 = pipeline_ws["tmp"] / "propcut2.yaml"
    cfg2.write_text(pipeline_ws["cfg"].read_text(encoding="utf-8")
                    .replace("resolution: 640x360", "resolution: 320x180"), encoding="utf-8")
    entry5 = run(cfg2, mode="process")[0]
    assert not entry5["success"] and not entry5["skipped"], entry5
    assert "不符" in (entry5["error"] or ""), entry5

    # 6) 意图不符（override 改起点）→ error，绝不能记 skipped-success
    (pipeline_ws["tmp"] / "overrides.json").write_text(
        json.dumps({"walkin_001.mp4": {"start_time": 2.0}}), encoding="utf-8")
    entry6 = run(pipeline_ws["cfg"], mode="process")[0]
    assert not entry6["success"] and not entry6["skipped"], entry6
    assert "不符" in (entry6["error"] or ""), entry6


# ================================================================ Round-2（Codex R3 round-1 findings F1-F10）


def test_f1_relock_changed_artifact_invalidates_downstream(tmp_path):
    """F1：人工改 EDL 后重新审批（set_lock）→ 基于旧审批的下游结果必须失效。"""
    out = _out(tmp_path)
    _prime_all_done_and_locked(out)
    edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
    edl["keep_ranges"][0]["src_end"] = 2.0  # 人工改剪点后重新 lock
    (out / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    set_lock(out, "p", "cut_lock", True)
    st = load_state(out, "p")
    for s in ("rough_cut", "fine_cut", "subtitle", "qa_export"):
        assert st["stages"][s]["status"] == "pending", f"{s} 应因 cut_lock 重新审批而失效"
    assert st["stages"]["paper_edit"]["status"] == "done"
    assert st["stages"]["transcribe"]["status"] == "done"
    assert isinstance(st["locks"]["cut_lock"], dict), "新审批记录本身要保留"
    assert st["locks"]["subtitle_lock"] is False, "字幕基于旧 EDL，旧审批必须作废"
    assert isinstance(st["locks"]["transcript_lock"], dict), "上游 transcript 审批不受影响"


def test_f1_idempotent_relock_keeps_downstream(tmp_path):
    """F1 边界：产物没变的重复上锁 = no-op，不得误伤下游。"""
    out = _out(tmp_path)
    _prime_all_done_and_locked(out)
    set_lock(out, "p", "cut_lock", True)
    st = load_state(out, "p")
    for s in ("rough_cut", "fine_cut", "subtitle", "qa_export"):
        assert st["stages"][s]["status"] == "done", f"产物未变的重复上锁不应失效 {s}"
    assert isinstance(st["locks"]["subtitle_lock"], dict)


def test_f1_unlock_invalidates_downstream(tmp_path):
    """F1：撤销审批（set_lock False）→ 下游结果失去审批依据，同样失效。"""
    out = _out(tmp_path)
    _prime_all_done_and_locked(out)
    set_lock(out, "p", "cut_lock", False)
    st = load_state(out, "p")
    assert st["locks"]["cut_lock"] is False
    for s in ("rough_cut", "fine_cut", "subtitle", "qa_export"):
        assert st["stages"][s]["status"] == "pending", f"撤销 cut_lock 后 {s} 应失效"
    assert st["locks"]["subtitle_lock"] is False


def test_f2_forged_out_start_rejected_by_make_srt(tmp_path):
    """F2：out_start 不在锁面内（T4 语义），但字幕消费它——被手改必须当场拒绝。"""
    from talkcut.subtitle import make_srt
    out = _out(tmp_path)
    _write_transcript(out)
    _write_edl(out)
    set_lock(out, "p", "transcript_lock", True)
    set_lock(out, "p", "cut_lock", True)
    edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
    edl["keep_ranges"][0]["out_start"] = 999.0
    (out / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    require_lock(out, "p", "cut_lock")  # 锁面确认不覆盖 out_start——正因如此才需要下面的消费端校验
    with pytest.raises(RuntimeError, match="out_start"):
        make_srt(out, "p")


def test_f3a_same_path_source_swap_breaks_cut_lock(tmp_path):
    """F3a：同路径同大小换源（重拍覆盖 + mtime 复原）→ 锁面并入源内容身份后必须破锁。"""
    out = _out(tmp_path)
    src = out / "src_real.mp4"
    src.write_bytes(b"A" * 4096)
    (out / "edl.json").write_text(json.dumps({
        "project": "p", "source_timeline": str(src), "human_locked": True,
        "keep_ranges": [{"src_start": 0.0, "src_end": 1.0, "label": "段"}],
        "cuts": [], "splices": [], "broll_cover": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    set_lock(out, "p", "cut_lock", True)
    require_lock(out, "p", "cut_lock")  # 未换源 → 放行
    stat = src.stat()
    src.write_bytes(b"B" * 4096)
    os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(RuntimeError, match="被改"):
        require_lock(out, "p", "cut_lock")


def test_f3b_require_lock_returns_verified_content(tmp_path):
    """F3b：require_lock 返回校验过的那份内容——消费方不再二次读盘（TOCTOU 窗口）。"""
    out = _out(tmp_path)
    _write_transcript(out)
    _write_edl(out)
    _write_srt(out)
    for lk in LOCKS:
        set_lock(out, "p", lk, True)
    transcript = require_lock(out, "p", "transcript_lock")
    assert isinstance(transcript, dict) and transcript["segments"][0]["text"] == "今天看房"
    edl = require_lock(out, "p", "cut_lock")
    assert isinstance(edl, dict) and edl["keep_ranges"][0]["src_end"] == 2.5
    srt = require_lock(out, "p", "subtitle_lock")
    assert isinstance(srt, str) and "今天看房" in srt


def test_f4_make_srt_requires_transcript_lock(tmp_path):
    """F4：字幕同时消费 transcript——只锁 EDL 时改词可静默进字幕，必须双锁。"""
    from talkcut.subtitle import make_srt
    out = _out(tmp_path)
    _write_transcript(out)
    _write_edl(out)
    set_lock(out, "p", "cut_lock", True)  # 只有 cut_lock，无 transcript_lock
    with pytest.raises(RuntimeError, match="transcript_lock"):
        make_srt(out, "p")


def test_f5_export_refuses_replaced_output(tmp_path):
    """F5：fine_cut 完成后文件被换 → 指纹不符，qa-export 拒绝选源。"""
    from talkcut.qaexport import _src_for_export
    out = _out(tmp_path)
    fine = out / "fine_cut.mp4"
    fine.write_bytes(b"encoded-bytes-v1")
    st = mark_stage(out, "p", "fine_cut", "done", [str(fine)], True)
    assert _src_for_export(out, st).name == "fine_cut.mp4"  # 未改动 → 放行
    fine.write_bytes(b"REPLACED-with-other-content!!")
    with pytest.raises(RuntimeError, match="指纹"):
        _src_for_export(out, load_state(out, "p"))


def test_f5_export_refuses_unverified_stage(tmp_path):
    """F5：status=done 但 verified=False → 未经校验的产物不出门。"""
    from talkcut.qaexport import _src_for_export
    out = _out(tmp_path)
    fine = out / "fine_cut.mp4"
    fine.write_bytes(b"encoded-bytes-v1")
    st = mark_stage(out, "p", "fine_cut", "done", [str(fine)], False)
    with pytest.raises(RuntimeError, match="verified"):
        _src_for_export(out, st)


def test_f5_export_refuses_legacy_state_without_identity(tmp_path):
    """F5：legacy state（无 outputs_identity）无法证明文件来历 → fail-closed。"""
    from talkcut.qaexport import _src_for_export
    out = _out(tmp_path)
    (out / "fine_cut.mp4").write_bytes(b"encoded-bytes-v1")
    st = _default_state_dict()
    st["stages"]["fine_cut"] = {"status": "done", "outputs": ["fine_cut.mp4"], "verified": True}
    with pytest.raises(RuntimeError, match="outputs_identity"):
        _src_for_export(out, st)


def test_f6_export_intent_covers_full_parameter_surface(tmp_path):
    """F6：会改变成片的每个参数（分辨率/fit/调色链/音乐面）都必须改变意图面。"""
    from propcut.pipeline import _export_intent

    src = tmp_path / "v.mp4"
    src.write_bytes(b"V" * 4096)
    entry = {"start_time_used": 1.5, "color_preset": "bright_interior", "hdr_tonemapped": False}
    gcfg = {"mode": "preset"}
    export_cfg = {"quality": "high", "resolution": "1920x1080", "fit": "pad"}
    music_cfg = {"original_audio": "remove", "volume": 0.7, "original_volume": 0.2,
                 "fade_in": 1.0, "fade_out": 1.5, "music_start": 0.0,
                 "profile": "default", "categories": ["calm"]}
    base = _export_intent(src, entry, gcfg, "eq=brightness=0.05", None, export_cfg, music_cfg)
    assert base["intent_version"] >= 2
    variants = [
        _export_intent(src, entry, gcfg, "eq=brightness=0.05", None,
                       {**export_cfg, "resolution": "1080x1920"}, music_cfg),
        _export_intent(src, entry, gcfg, "eq=brightness=0.05", None,
                       {**export_cfg, "fit": "crop"}, music_cfg),
        _export_intent(src, entry, gcfg, "eq=contrast=1.2", None, export_cfg, music_cfg),
        _export_intent(src, entry, gcfg, "eq=brightness=0.05", None, export_cfg,
                       {**music_cfg, "original_audio": "keep"}),
        _export_intent(src, entry, gcfg, "eq=brightness=0.05", None, export_cfg,
                       {**music_cfg, "fade_out": 0.0}),
    ]
    for i, v in enumerate(variants):
        assert v != base, "variant %d 改了会影响成片的参数但意图面没变" % i


def test_f8_identity_algo_and_analyzer_version_gate(tmp_path):
    """F8：src_identity 带算法版本字段；检测缓存缺 analyzer_version 不得复用。"""
    from propcut.pipeline import ANALYZER_VERSION, DETECT_CFG_KEYS, _cache_matches
    from propcut.utils import src_identity

    src = tmp_path / "v.mp4"
    src.write_bytes(b"A" * 4096)
    ident = src_identity(src)
    assert ident.get("algo"), "src_identity 必须带算法版本字段（采样指纹的碰撞边界要可追溯）"

    detect_cfg = {"scan_seconds": 10, "sample_fps": 2, "stable_window": 1.0, "max_trim_seconds": 20}
    cached = {"video": str(src), "probe": {"duration_sec": 10.0},
              "detect_cfg": {k: detect_cfg.get(k) for k in DETECT_CFG_KEYS},
              "src_identity": ident, "analyzer_version": ANALYZER_VERSION}
    assert _cache_matches(cached, src, 10.0, detect_cfg) is True
    stale = {k: v for k, v in cached.items() if k != "analyzer_version"}
    assert _cache_matches(stale, src, 10.0, detect_cfg) is False, \
        "缺 analyzer_version 的旧缓存不能复用（检测算法升级后旧裁点失效）"


def test_f9_tmp_write_failure_cleans_up(tmp_path, monkeypatch):
    """F9：写临时文件中途失败 → 原 state 完好 + 不留 .tmp 残骸（写入须在 try 内）。"""
    out = _out(tmp_path)
    mark_stage(out, "p", "ingest", "done", [], True)
    original = state_path(out).read_text(encoding="utf-8")
    st = load_state(out, "p")

    real_write_text = Path.write_text

    def boom(self, *args, **kwargs):
        if ".tmp" in self.name:
            real_write_text(self, "partial", encoding="utf-8")  # 模拟写一半（文件已创建）
            raise OSError("disk full mid-write")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        save_state(out, st)
    monkeypatch.undo()
    assert state_path(out).read_text(encoding="utf-8") == original, "失败后原 state.json 必须完好"
    assert not list(out.glob("*.tmp*")), "写失败后不留 .tmp 残骸"


def test_f9_tmp_name_unique_per_write(tmp_path, monkeypatch):
    """F9：固定 .tmp 名并发互覆——tmp 名必须每次唯一（pid+序号）。"""
    out = _out(tmp_path)
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    mark_stage(out, "p", "ingest", "done", [], True)
    mark_stage(out, "p", "transcribe", "done", [], True)
    assert len(seen) >= 2 and seen[0] != seen[1], "固定 .tmp 名会被并发写者互覆：%r" % seen


@pytest.mark.parametrize("field, value, needle", [
    ("artifact", 123, "artifact"),
    ("sha256", "xyz", "sha256"),
    ("approved_at", 0, "approved_at"),
])
def test_f10_forged_lock_record_field_types_rejected(tmp_path, field, value, needle):
    """F10：审批记录字段只查存在不查类型/格式 → 伪造记录能过校验。"""
    out = _out(tmp_path)
    d = _default_state_dict()
    rec = {"artifact": "edl.json", "sha256": "0" * 64, "approved_at": "2026-07-17T00:00:00+00:00"}
    rec[field] = value
    d["locks"]["cut_lock"] = rec
    state_path(out).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=needle):
        load_state(out, "p")


def test_f10_lock_record_artifact_mismatch_rejected(tmp_path):
    """F10：审批记录 artifact 字段被手改 → require_lock 不能只比 sha256。"""
    out = _out(tmp_path)
    _write_edl(out)
    set_lock(out, "p", "cut_lock", True)
    st = json.loads(state_path(out).read_text(encoding="utf-8"))
    st["locks"]["cut_lock"]["artifact"] = "别的文件.json"
    state_path(out).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="artifact"):
        require_lock(out, "p", "cut_lock")


def test_f10_save_state_validates_before_write(tmp_path):
    """F10：save_state 必须校验——非法 state 不得原子覆盖健康文件。"""
    out = _out(tmp_path)
    mark_stage(out, "p", "ingest", "done", [], True)
    original = state_path(out).read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        save_state(out, {"bad": 1})
    assert state_path(out).read_text(encoding="utf-8") == original, "非法 state 不得覆盖健康文件"


def test_f10_invalid_utf8_wrapped_with_path(tmp_path):
    """F10：非法 UTF-8 的 state 文件 → 带路径的 ValueError（不裸抛 UnicodeDecodeError）。"""
    out = _out(tmp_path)
    state_path(out).write_bytes(b"\xff\xfe\x01broken")
    with pytest.raises(ValueError, match="state.json"):
        load_state(out, "p")


# ---------------------------------------------------------------- Round-3（Codex round-2 P1-1/P1-2/P1-3）


def _finecut_ws(tmp_path: Path) -> Path:
    """no-B-roll fine-cut 工作台：edl + rough 占位 + cut_lock + rough_cut done（含指纹）。"""
    out = _out(tmp_path)
    (out / "edl.json").write_text(json.dumps({
        "source_timeline": str(tmp_path / "src.mp4"),
        "keep_ranges": [{"src_start": 0.0, "src_end": 5.0}],
        "cuts": [], "splices": [], "broll_cover": [], "human_locked": True,
    }, ensure_ascii=False), encoding="utf-8")
    (out / "rough_cut.mp4").write_bytes(b"R" * 4096)
    set_lock(out, "t", "cut_lock", True)
    mark_stage(out, "t", "rough_cut", "done", [str(out / "rough_cut.mp4")], verified=True)
    return out


def test_p1_finecut_rejects_invalidated_rough(tmp_path):
    """P1-1：改 EDL 重新审批 → rough_cut 失效回 pending，旧 rough 仍在盘上，
    fine-cut 不得消费（否则 qa-export 三重门会认可这个新登记的 fine，导出旧剪点）。"""
    from talkcut import finecut

    out = _finecut_ws(tmp_path)
    assert finecut.fine_cut(out, "t").exists()  # 基线：门通过，no-covers 拷贝成功

    edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
    edl["keep_ranges"] = [{"src_start": 0.0, "src_end": 3.0}]
    (out / "edl.json").write_text(json.dumps(edl, ensure_ascii=False), encoding="utf-8")
    set_lock(out, "t", "cut_lock", True)  # 重新审批 → 下游失效
    with pytest.raises(RuntimeError, match="rough_cut"):
        finecut.fine_cut(out, "t")


def test_p1_finecut_rejects_replaced_rough(tmp_path):
    """P1-1：rough_cut done 后文件被同名替换（同尺寸异内容）→ fine-cut 拒绝消费。"""
    from talkcut import finecut

    out = _finecut_ws(tmp_path)
    (out / "rough_cut.mp4").write_bytes(b"X" * 4096)
    with pytest.raises(RuntimeError, match="替换|被改|不符"):
        finecut.fine_cut(out, "t")


def test_p1_finecut_rejects_legacy_state_without_identity(tmp_path):
    """P1-1：legacy state（done 但无 outputs_identity）→ fail-closed 提示重跑，不消费。"""
    from talkcut import finecut

    out = _finecut_ws(tmp_path)
    st = json.loads(state_path(out).read_text(encoding="utf-8"))
    st["stages"]["rough_cut"] = {"status": "done",
                                 "outputs": [str(out / "rough_cut.mp4")], "verified": True}
    state_path(out).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="outputs_identity|重跑"):
        finecut.fine_cut(out, "t")


def test_p1_export_intent_covers_audio_pipeline_switches(tmp_path):
    """P1-2：music.enabled / loudnorm 开关与目标值改变最终音频链 → 必须改变意图面。"""
    from propcut.pipeline import _export_intent

    src = tmp_path / "v.mp4"
    src.write_bytes(b"V" * 4096)
    entry = {"start_time_used": 1.5, "color_preset": "bright_interior", "hdr_tonemapped": False}
    gcfg = {"mode": "preset", "preset": "bright_interior"}
    export_cfg = {"quality": "medium", "resolution": None, "fit": "contain"}
    ln = {"enabled": False, "i": -14.0, "tp": -1.5, "lra": 11.0}
    m = {"enabled": True, "original_audio": "remove", "volume": 0.7, "original_volume": 0.2,
         "fade_in": 1.0, "fade_out": 1.5, "music_start": 0.0,
         "profile": "default", "categories": ["calm"],
         "select": "random", "file": None, "category": None, "loudnorm": ln}
    base = _export_intent(src, entry, gcfg, "eq=b=0.05", None, export_cfg, m)
    variants = [
        _export_intent(src, entry, gcfg, "eq=b=0.05", None, export_cfg, {**m, "enabled": False}),
        _export_intent(src, entry, gcfg, "eq=b=0.05", None, export_cfg,
                       {**m, "loudnorm": {**ln, "enabled": True}}),
        _export_intent(src, entry, gcfg, "eq=b=0.05", None, export_cfg,
                       {**m, "loudnorm": {**ln, "i": -16.0}}),
    ]
    for i, v in enumerate(variants):
        assert v != base, f"变体 {i}（enabled/loudnorm 面）没有改变意图"


def test_p1_export_intent_covers_explicit_selection(tmp_path):
    """P1-3：确定性选曲请求（select/file/category/override）改了 = 不同意图；
    随机选曲的结果（具体选中哪首）不进意图面。"""
    from propcut.pipeline import _export_intent

    src = tmp_path / "v.mp4"
    src.write_bytes(b"V" * 4096)
    entry = {"start_time_used": 1.5, "color_preset": None, "hdr_tonemapped": False}
    gcfg = {"mode": "none", "preset": None}
    export_cfg = {"quality": "medium", "resolution": None, "fit": "contain"}
    m = {"enabled": True, "original_audio": "remove", "volume": 0.8, "original_volume": 0.0,
         "fade_in": 0.0, "fade_out": 0.0, "music_start": 0.0, "profile": None, "categories": None,
         "select": "random", "file": None, "category": None,
         "loudnorm": {"enabled": False, "i": -14.0, "tp": -1.5, "lra": 11.0}}
    base = _export_intent(src, entry, gcfg, None, None, export_cfg, m)
    variants = [
        _export_intent(src, entry, gcfg, None, None, export_cfg,
                       {**m, "select": "file", "file": "a.mp3"}),
        _export_intent(src, entry, gcfg, None, None, export_cfg,
                       {**m, "select": "category", "category": "calm"}),
        _export_intent(src, entry, gcfg, None, None, export_cfg, m,
                       override_music="b.mp3"),
    ]
    for i, v in enumerate(variants):
        assert v != base, f"变体 {i}（显式选曲面）没有改变意图"


def test_p1_sidecar_selected_music_provenance_and_override_breaks_skip(pipeline_ws):
    """P1-3：sidecar 必须记录实际选中曲目（provenance，不参与 skip 比对）；
    per-video override 换曲 → overwrite=false 跳过判定按意图不符报错。"""
    from propcut.pipeline import run

    entry = run(pipeline_ws["cfg"], mode="process")[0]
    assert entry["success"] and not entry["error"], entry
    sc = json.loads((pipeline_ws["tmp"] / "out" / "_reports" / "walkin_001.output.json")
                    .read_text(encoding="utf-8"))
    sm = sc.get("selected_music")
    assert sm and sm["name"] == "tone.wav", f"sidecar 缺实际曲目 lineage: {sm!r}"
    assert {"size", "quick_hash", "algo"} <= set(sm.get("identity") or {}), sm

    other = pipeline_ws["tmp"] / "music" / "calm" / "tone2.wav"
    _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=880:duration=3", str(other)])
    (pipeline_ws["tmp"] / "overrides.json").write_text(
        json.dumps({"walkin_001.mp4": {"music": str(other)}}), encoding="utf-8")
    entry2 = run(pipeline_ws["cfg"], mode="process")[0]
    assert not entry2["success"] and not entry2["skipped"], entry2
    assert "不符" in (entry2["error"] or ""), entry2


# ---------------------------------------------------------------- Round-4（Codex round-3 P1：曲库归一进意图面）


def _library_switch_intent(tmp_path: Path, **selection) -> tuple[dict, dict]:
    """同一确定性选曲请求在曲库 A / 曲库 B 下的两份 intent（其余参数全同）。"""
    from propcut.pipeline import _export_intent

    src = tmp_path / "v.mp4"
    src.write_bytes(b"V" * 4096)
    entry = {"start_time_used": 1.5, "color_preset": None, "hdr_tonemapped": False}
    gcfg = {"mode": "none", "preset": None}
    export_cfg = {"quality": "medium", "resolution": None, "fit": "contain"}
    override = selection.pop("override_music", None)
    m = {"enabled": True, "original_audio": "remove", "volume": 0.8, "original_volume": 0.0,
         "fade_in": 0.0, "fade_out": 0.0, "music_start": 0.0, "profile": None, "categories": None,
         "select": "random", "file": None, "category": None,
         "library": str(tmp_path / "libA"),
         "loudnorm": {"enabled": False, "i": -14.0, "tp": -1.5, "lra": 11.0},
         **selection}
    a = _export_intent(src, entry, gcfg, None, None, export_cfg, m, override_music=override)
    b = _export_intent(src, entry, gcfg, None, None, export_cfg,
                       {**m, "library": str(tmp_path / "libB")}, override_music=override)
    return a, b


def test_p1_export_intent_covers_library_switch_for_file(tmp_path):
    """round-3 P1：相对选曲按当前曲库解析（music.py 把 wanted 拼到 library 下）——
    换曲库、同相对文件名 = 物理曲目已变，intent 必须不同，否则旧库成片被 skipped-success 误认。"""
    a, b = _library_switch_intent(tmp_path, select="file", file="calm/tone.wav")
    assert a != b, "换曲库（select=file 同相对文件名）没有改变意图"


def test_p1_export_intent_covers_library_switch_for_override(tmp_path):
    """round-3 P1：per-video override_music 相对路径同样按曲库解析 → 换曲库必须改变意图。"""
    a, b = _library_switch_intent(tmp_path, override_music="calm/tone.wav")
    assert a != b, "换曲库（override 同相对文件名）没有改变意图"
