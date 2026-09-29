"""talkcut CLI — 7 阶段 + lock + smoke。

ingest / transcribe / paper-edit / rough-cut / fine-cut / subtitle / qa-export
人审锁：transcript_lock / cut_lock / subtitle_lock（lock 子命令上锁）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from estatecut.ffmpeg_tools import run_video_encode
from estatecut.resources_path import resource_path

from . import ingest as ingest_mod
from . import transcribe as transcribe_mod
from . import paperedit as paperedit_mod
from . import cut as cut_mod
from . import finecut as finecut_mod
from . import subtitle as subtitle_mod
from . import qaexport as qaexport_mod
from .state import _atomic_write_json, set_lock, load_state


def _save_ingest(out: Path, summary: dict) -> None:
    (out / "ingest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_ingest(out: Path) -> dict:
    return json.loads((out / "ingest.json").read_text(encoding="utf-8"))


def cmd_ingest(a):
    s = ingest_mod.ingest(Path(a.input), Path(a.out), a.project)
    _save_ingest(Path(a.out), s)
    print(f"[ingest] {len(s['clips'])} 段 → {s['duration_sec']}s  {s['source']}")


def cmd_transcribe(a):
    t = transcribe_mod.transcribe(Path(a.out), a.project, _load_ingest(Path(a.out)), mode=a.mode, device=a.device)
    print(f"[transcribe] {len(t['segments'])} segs / {len(t['gap_audit'])} gaps；mode={a.mode}")


def cmd_paper_edit(a):
    # propose_edl 只产出起步骨架：keep=全片、cuts=[]（AI 不自动剪任何未确认段，见 talkcut/paperedit.py 模块 docstring）。
    # 逐段 KEEP/CUT/DEDUP 判断由 agent 读 prompts/paperedit_prompt.md 后编辑 edl.json，人审上锁才进 rough-cut。
    e = paperedit_mod.propose_edl(Path(a.out), a.project)
    print(f"[paper-edit] 起步骨架：{len(e['keep_ranges'])} 保留区间（全片）/ {len(e['cuts'])} cuts（本命令不自动剪，待 agent 逐段判断）")
    print(f"[paper-edit] 参考指南：{resource_path('paperedit_prompt.md')}；编辑 edl.json 后人审 `talkcut lock cut_lock`")


def cmd_lock(a):
    out = Path(a.out)
    # cut_lock：先原子回写 edl.human_locked=True 再上锁——锁 hash 必须盖在最终内容上
    # （旧顺序先 set_lock 再回写，回写后 require_lock 立刻误报"被改"）
    if a.name == "cut_lock" and (out / "edl.json").exists():
        edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
        edl["human_locked"] = True
        _atomic_write_json(out / "edl.json", edl)
        set_lock(out, a.project, a.name, True)
        print("[lock] edl.human_locked = True + cut_lock = 审批记录（hash 盖最终内容）")
    else:
        set_lock(out, a.project, a.name, True)
        print(f"[lock] {a.name} = 审批记录")


def cmd_rough_cut(a):
    p = cut_mod.rough_cut(Path(a.out), a.project, scale_h=a.scale_h)
    print(f"[rough-cut] {p}")


def cmd_fine_cut(a):
    p = finecut_mod.fine_cut(Path(a.out), a.project)
    print(f"[fine-cut] {p}")


def cmd_subtitle(a):
    p = subtitle_mod.make_srt(Path(a.out), a.project)
    print(f"[subtitle] {p}（未烧录）")


def cmd_qa_export(a):
    r = qaexport_mod.qa_export(Path(a.out), a.project, slug=a.slug)
    print(f"[qa-export] {len(r['exports'])} 版本；违禁词命中 {len(r['compliance_hits'])}；"
          f"verdict={r['verdict']}")
    # R4 round-2 P1-1：QA 非 PASS 就非零退出——CLI 层不许把 FAIL/PARTIAL 吞成成功
    return 0 if r["verdict"] == "PASS" else 1


def cmd_self_eval(a):
    from . import self_eval as se_mod  # lazy：只有跑自检才依赖 PIL/numpy
    r = se_mod.self_eval(Path(a.out), a.project, window=a.window)
    status = "OK" if r["duration_ok"] else f"时长不符 Δ{r['duration_delta_s']}s"
    print(f"[self-eval] {r['video']} {status}；{len(r['eval_images'])} 张自检图 → {a.out}/verify/self_eval/")


def cmd_timeline_view(a):
    from . import timeline_view as tv_mod  # lazy
    out = tv_mod.render_timeline(Path(a.video), a.start, a.end,
                                 Path(a.transcript) if a.transcript else None,
                                 a.n_frames, Path(a.output) if a.output else None)
    print(f"[timeline-view] {out}")


def cmd_smoke(a):
    sys.exit(_smoke(Path(a.out)))


def _synth_clip(path: Path, dur: float, label: str):
    run_video_encode(lambda enc: ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30:duration={dur}",
                 "-f", "lavfi", "-i", f"sine=frequency=300:duration={dur}",
                 *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path), "-y"])


def _smoke(out: Path) -> int:
    import tempfile, shutil
    proj = "smoke"
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="talkcut_smoke_"))
    try:
        _synth_clip(tmp / "DJI_001.mp4", 3.0, "a")
        _synth_clip(tmp / "DJI_002.mp4", 3.0, "b")
        s = ingest_mod.ingest(tmp, out, proj); _save_ingest(out, s)
        transcribe_mod.transcribe(out, proj, s, mode="mock")
        # R4 Q8：合成音在 mock 下必出 retranscribe gap——模拟人工听审确认
        #（加 resolution）后才允许上 transcript_lock，顺带把门控走一遍
        tpath = out / "transcript.json"
        t = json.loads(tpath.read_text(encoding="utf-8"))
        for g in t.get("gap_audit", []):
            if g.get("decision") == "retranscribe" and not g.get("resolution"):
                g["resolution"] = "smoke：合成素材无口播，人审模拟确认"
        tpath.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
        set_lock(out, proj, "transcript_lock", True)   # 模拟人审转录稿
        paperedit_mod.propose_edl(out, proj)
        # 模拟人审：构造 2 段保留（测 trim/concat 多段路径）+ human_locked + cut_lock
        edl = json.loads((out / "edl.json").read_text(encoding="utf-8"))
        edl["keep_ranges"] = [{"src_start": 0.0, "src_end": 2.5, "label": "段1"},
                              {"src_start": 3.5, "src_end": 5.5, "label": "段2"}]
        edl["cuts"] = [{"src_start": 2.5, "src_end": 3.5, "category": "dead_air", "reason": "smoke 测试剪点", "transcript_quote": ""}]
        edl["human_locked"] = True
        (out / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
        set_lock(out, proj, "cut_lock", True)
        cut_mod.rough_cut(out, proj, scale_h=1080)
        subtitle_mod.make_srt(out, proj)
        set_lock(out, proj, "subtitle_lock", True)  # R4 Q7：qa_export 门控要求字幕人审锁（smoke 模拟）
        finecut_mod.fine_cut(out, proj)
        from . import self_eval as se_mod  # self-eval 自检（借鉴 video-use）：成片剪点出图 + 时长核对
        se_mod.self_eval(out, proj)
        qa = qaexport_mod.qa_export(out, proj, slug=proj)

        # 验证
        need = ["source.mp4", "audio.wav", "transcript.json", "edl.json", "粗剪报告.md",
                "rough_cut.mp4", "字幕.srt", "fine_cut.mp4", "verify/self_eval/self_eval_report.json",
                "exports/smoke_vertical_9x16.mp4", "exports/smoke_master_landscape.mp4", "exports/smoke_proxy.mp4"]
        missing = [f for f in need if not (out / f).exists()]
        st = load_state(out, proj)
        all_done = all(st["stages"][k]["status"] == "done" for k in ["ingest", "transcribe", "paper_edit", "rough_cut", "fine_cut", "subtitle", "qa_export"])
        # R4 round-2 P1-1：文件齐全 ≠ QA 过——smoke 还要求 qa verdict=PASS 且
        # qa_export 阶段 verified=True（qa_export 是唯一带机器验证语义的阶段）
        qa_ok = qa["verdict"] == "PASS" and st["stages"]["qa_export"]["verified"] is True
        if missing or not all_done or not qa_ok:
            print(f"[smoke] FAIL  missing={missing}  all_done={all_done}  "
                  f"qa_verdict={qa['verdict']}  qa_verified={st['stages']['qa_export']['verified']}")
            return 1
        print(f"[smoke] PASS  全 7 阶段产物齐全 + state 全 done + QA verdict=PASS  → {out}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_parser():
    p = argparse.ArgumentParser(prog="talkcut", description="中文口播视频剪辑助手（7 阶段）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, project_required=True):
        sp.add_argument("--out", required=True)
        sp.add_argument("--project", required=project_required, default="proj")

    sp = sub.add_parser("ingest"); sp.add_argument("--input", required=True); common(sp); sp.set_defaults(fn=cmd_ingest)
    sp = sub.add_parser("transcribe"); common(sp); sp.add_argument("--mode", required=True, choices=["real", "mock"], help="real=显式真跑 faster-whisper；mock=测试"); sp.add_argument("--device", default="cuda"); sp.set_defaults(fn=cmd_transcribe)
    sp = sub.add_parser("paper-edit", help="产出剪辑起步骨架（keep=全片 / cuts=[]）供 agent 逐段编辑 edl.json；命令本身不自动剪", description="产出剪辑起步骨架：edl.json（keep=全片 / cuts=[]，不自动剪任何未确认段）+ 粗剪报告.md（gap 待审候选骨架 + 「人工精判清单（agent 补）」占位）。逐段 KEEP/CUT/DEDUP 精判由 agent 读 prompts/paperedit_prompt.md 后编辑 edl.json + 补全粗剪报告.md，人审 `talkcut lock cut_lock` 才进 rough-cut。"); common(sp); sp.set_defaults(fn=cmd_paper_edit)
    sp = sub.add_parser("lock"); sp.add_argument("name", choices=["transcript_lock", "cut_lock", "subtitle_lock"]); common(sp); sp.set_defaults(fn=cmd_lock)
    sp = sub.add_parser("rough-cut"); common(sp); sp.add_argument("--scale-h", type=int, default=1080); sp.set_defaults(fn=cmd_rough_cut)
    sp = sub.add_parser("fine-cut"); common(sp); sp.set_defaults(fn=cmd_fine_cut)
    sp = sub.add_parser("subtitle"); common(sp); sp.set_defaults(fn=cmd_subtitle)
    sp = sub.add_parser("qa-export"); common(sp); sp.add_argument("--slug", default=None); sp.set_defaults(fn=cmd_qa_export)
    sp = sub.add_parser("self-eval"); common(sp); sp.add_argument("--window", type=float, default=1.5); sp.set_defaults(fn=cmd_self_eval)
    sp = sub.add_parser("timeline-view"); sp.add_argument("--video", required=True); sp.add_argument("--start", type=float, required=True); sp.add_argument("--end", type=float, required=True); sp.add_argument("--transcript", default=None); sp.add_argument("--n-frames", type=int, default=8); sp.add_argument("--output", default=None); sp.set_defaults(fn=cmd_timeline_view)
    sp = sub.add_parser("smoke"); sp.add_argument("--out", default="out/smoke_talkcut"); sp.set_defaults(fn=cmd_smoke)
    return p


def main(argv=None):
    # Windows 默认 cp1252/GBK 控制台下中文帮助/报错会 UnicodeEncodeError——强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
