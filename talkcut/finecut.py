"""§5 fine-cut — B-roll 盖跳切（只盖画面不换主音频）。

移植自 地下室如何看房 filtergraph_finalcut overlay 思路。
B-roll 取自 source（被剪区段全新画面，按 rough 高度 scale → 4K rough 自动出 4K B-roll，无上采样）。
edl.broll_cover 为空 → fine_cut = rough_cut 拷贝（合法 no-op）。

audio 是 `-c:a copy`：**剪点 micro-fade 已在 rough-cut（cut.py build_filtergraph）做掉**，
fine-cut 直接 copy rough 的音频、自然继承 fade，本阶段不再处理音频（12ms qsin afade 方案见 cut.py / methodology.md §5）。
注："增量 overlay" 经重核为伪需求（fine-cut 本就 = rough + overlay 全部 broll，改 broll 列表重跑即等价）。详见 docs/IMPLEMENTATION-PLAN.md 回灌进展。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from estatecut.ffmpeg_tools import probe_media, run_video_encode

from .state import mark_stage, require_lock, require_stage_output


def fine_cut(out_dir: Path, project: str, allow_unlocked: bool = False, log: Path | None = None) -> Path:
    out_dir = Path(out_dir)
    if not allow_unlocked:
        # B-roll 盖跳切属剪辑层，同 cut_lock 门控；用 require_lock 返回的校验内容，不二次读盘
        edl = require_lock(out_dir, project, "cut_lock")
    else:
        edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
    fine = out_dir / "fine_cut.mp4"

    covers = edl.get("broll_cover", [])
    # 预检：每条 broll_cover 必须有 reason（硬约束 7：空镜要和当下口播相关、
    # 不制造事实误导）。这是 EDL 层错误，与 rough_cut 状态无关 → 先于产物门 fail fast。
    for cv in covers:
        if not str(cv.get("reason", "")).strip():
            raise ValueError(f"broll_cover 缺 reason（硬约束 7：空镜要和当下口播相关、不制造事实误导，必须说明理由）: {cv}")

    # rough_cut 消费门（Codex R3 P1-1）：上游重跑/重新审批会把 rough_cut 置回 pending，
    # 但旧 rough_cut.mp4 还在盘上——只查存在会把旧剪点成片拷成"新" fine 再被 qa 认可
    rough = require_stage_output(out_dir, project, "rough_cut", "rough_cut.mp4")

    if not covers:
        shutil.copyfile(rough, fine)  # 无跳切覆盖 → 直接拷贝（音频画面均不动）
        mark_stage(out_dir, project, "fine_cut", "done", [str(fine)], verified=fine.exists(), note="无 B-roll，= rough_cut")
        return fine

    source = Path(edl["source_timeline"])
    # B-roll 按 rough_cut 实际高度缩放（rough-cut 可能用了非 1080 的 --scale-h）
    rough_h = int(probe_media(rough)["height"]) or 1080
    inputs = ["-i", str(rough)]
    setpts, overlays = [], []
    prev = "[0:v]"
    n = len(covers)
    for i, cv in enumerate(covers, start=1):
        win_a = float(cv["out_window_start"]); win_b = float(cv["out_window_end"])
        if win_b <= win_a:
            raise ValueError(f"broll_cover 窗口非法 end<=start: {cv}")
        bsrc = float(cv["broll_src_start"]); dur = win_b - win_a + 0.4
        inputs += ["-ss", f"{bsrc:.3f}", "-t", f"{dur:.3f}", "-i", str(source)]
        setpts.append(f"[{i}:v]setpts=PTS-STARTPTS+{win_a}/TB,scale=-2:{rough_h}[b{i}];")
        out = "[outv]" if i == n else f"[v{i}]"   # 最后一个直接输出 outv
        overlays.append(f"{prev}[b{i}]overlay=enable='between(t,{win_a},{win_b})'{out};")
        prev = out
    graph = "\n".join(setpts + overlays).rstrip(";")

    work = out_dir / "_work"; work.mkdir(exist_ok=True)
    fg = work / "filtergraph_finecut.txt"; fg.write_text(graph, encoding="utf-8")
    run_video_encode(lambda enc: [
        "ffmpeg", "-hide_banner", "-loglevel", "error", *inputs,
        "-filter_complex_script", str(fg), "-map", "[outv]", "-map", "0:a",
        *enc,
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(fine), "-y",
    ], log)
    mark_stage(out_dir, project, "fine_cut", "done", [str(fine)], verified=fine.exists(),
               note=f"{len(covers)} 处 B-roll 盖跳切，音频 copy 不动")
    return fine
