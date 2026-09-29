"""§verify — 成片自检：剪点抽帧图 + 时长核对（用 timeline_view）。

借鉴 video-use SKILL.md self-eval loop（评估见 docs/references/video-use-eval-20260701.md）：
对渲染成品（**非源**）每个剪点边界 ±window 出 timeline_view 图，看有无视觉跳切 /
音频 spike / 字幕遮挡；再 ffprobe 核成片时长匹配 EDL 预期。

本 helper **只产证据**（图 + 时长核对 JSON），修不修由人/agent 定（cap 3 轮思路见 workflow.md §7）。
成片时间轴 ≠ source 时间轴，故 timeline_view 不带 transcript（只 filmstrip+波形+静音，
足够看接缝跳切/爆音；词标属 source 时间轴不适用成片）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from estatecut.ffmpeg_tools import probe_media

from .timeline_view import render_timeline


def output_cut_boundaries(edl: dict) -> list[float]:
    """成片时间轴上的剪点位置 = keep_ranges 各段 out_start（rough_cut 回填），跳过首段(=0)。"""
    keep = edl.get("keep_ranges", [])
    return [float(kr["out_start"]) for kr in keep[1:] if "out_start" in kr]


def self_eval(out_dir: Path, project: str, window: float = 1.5,
              prefer: str = "fine_cut.mp4") -> dict:
    out_dir = Path(out_dir)
    video = None
    for name in (prefer, "rough_cut.mp4"):
        if (out_dir / name).exists():
            video = out_dir / name
            break
    if video is None:
        raise FileNotFoundError("无 fine_cut/rough_cut 可自检")

    edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
    keep = edl.get("keep_ranges", [])

    # 时长核对：EDL 保留段总时长 vs 成片实际时长
    expected = sum(float(k["src_end"]) - float(k["src_start"]) for k in keep) if keep else 0.0
    actual = float(probe_media(video)["duration_sec"])
    dur_tol = max(0.15, expected * 0.02)
    duration_ok = abs(actual - expected) <= dur_tol

    eval_dir = out_dir / "verify" / "self_eval"
    eval_dir.mkdir(parents=True, exist_ok=True)

    images: list[str] = []
    # 每个剪点边界 ±window 出图（看接缝跳切/爆音）
    for b in output_cut_boundaries(edl):
        a, z = max(0.0, b - window), min(actual, b + window)
        if z > a:
            p = render_timeline(video, a, z, transcript=None, n_frames=6,
                                out_path=eval_dir / f"cut_{b:.2f}.png")
            images.append(str(p))
    # 首 / 尾 / 中 采样（grade 一致性、整体连贯）
    for tag, a, z in (("head", 0.0, min(2.0, actual)),
                      ("tail", max(0.0, actual - 2.0), actual),
                      ("mid", max(0.0, actual / 2 - 1.0), min(actual, actual / 2 + 1.0))):
        if z > a:
            p = render_timeline(video, a, z, transcript=None, n_frames=6,
                                out_path=eval_dir / f"sample_{tag}.png")
            images.append(str(p))

    report = {
        "video": video.name,
        "expected_duration_s": round(expected, 2),
        "actual_duration_s": round(actual, 2),
        "duration_ok": duration_ok,
        "duration_delta_s": round(actual - expected, 3),
        "cut_boundaries": output_cut_boundaries(edl),
        "eval_images": images,
        "note": "看每张图确认无视觉跳切/波形 spike（爆音）/字幕遮挡；时长不符先查 EDL vs 渲染",
    }
    (eval_dir / "self_eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="成片自检：剪点抽帧图 + 时长核对")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("project", type=str)
    ap.add_argument("--window", type=float, default=1.5)
    ap.add_argument("--prefer", type=str, default="fine_cut.mp4")
    args = ap.parse_args()
    rep = self_eval(args.out_dir, args.project, args.window, args.prefer)
    status = "OK" if rep["duration_ok"] else f"时长不符 Δ{rep['duration_delta_s']}s"
    print(f"[self_eval] {rep['video']} {status}；{len(rep['eval_images'])} 张自检图 → {args.out_dir}/verify/self_eval/")


if __name__ == "__main__":
    main()
