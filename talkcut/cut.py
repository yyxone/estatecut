"""§4 rough-cut — EDL → 帧精确 trim/concat 渲染，保留音频。

移植自 地下室如何看房 filtergraph_v3 实战。内容剪点在非关键帧 → 重编码（NVENC 优先）。
门控：需 cut_lock（人审剪点）才跑，除非 allow_unlocked。
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from estatecut.ffmpeg_tools import ffprobe_json, probe_media, run_video_encode

from .state import mark_stage, require_lock


def _fmt(x: float) -> str:
    # fade/st 秒数格式化：6 位小数去尾零。防 clamp 的 dur/2 长浮点尾巴 + 极小值科学计数法（ffmpeg 不识 1e-05）。
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}  # HLG, PQ（DJI/iPhone HDR 常见）


def _source_color_transfer(source: Path) -> str:
    try:
        data = ffprobe_json(source)
    except Exception:
        return ""
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return (s.get("color_transfer") or "").lower()
    return ""


def _zscale_available() -> bool:
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                              capture_output=True, text=True, shell=False)
        return "zscale" in (proc.stdout + proc.stderr)
    except OSError:
        return False


def hdr_tonemap_chain(source: Path) -> str:
    """source 为 HLG/PQ HDR 且 zscale 可用 → 返回 HDR→SDR tonemap 滤镜串；否则空串（不处理）。

    借鉴 video-use render.py is_hdr_source+tonemap（评估见 docs/references/video-use-eval-20260701.md）。
    defensive：SDR 源返回空串 → 渲染路径零改动；无 zscale 也返回空串（不强插避免炸）。
    DJI Pocket / iPhone 可产 HLG，但**非默认**——只有 ffprobe 实测命中 color_transfer 才处理。
    """
    ct = _source_color_transfer(source)
    if ct not in HDR_TRANSFERS:
        return ""
    if not _zscale_available():
        return ""
    return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def build_filtergraph(keep_ranges: list[dict], scale_h: int | None = None,
                      crossfade: float = 0.012, tonemap: str = "") -> tuple[str, str]:
    # crossfade: 剪点接缝微 fade 秒数。非首段头 fade in、非尾段尾 fade out，
    # butt-join 不重叠、不改时长、音视频严格同步，消硬切 click。0=关。
    # 选型见 docs/methodology.md §5（不用 acrossfade——会叠音+破坏同步）。移植自 _recover/build_audio.py。
    lines, labels = [], []
    n = len(keep_ranges)
    for i, kr in enumerate(keep_ranges):
        a, b = float(kr["src_start"]), float(kr["src_end"])
        dur = b - a
        lines.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];")
        af = ""
        if crossfade > 0 and dur > 0:
            head, tail = i > 0, i < n - 1
            # clamp 防负 st：中间段头尾各 fade ≤ dur/2（不互相压满），首/尾段单边 ≤ dur
            fade = min(crossfade, dur / 2) if (head and tail) else min(crossfade, dur)
            fades = []
            if head:
                fades.append(f"afade=t=in:st=0:d={_fmt(fade)}:curve=qsin")
            if tail:
                fades.append(f"afade=t=out:st={_fmt(max(0.0, dur - fade))}:d={_fmt(fade)}:curve=qsin")
            if fades:
                af = "," + ",".join(fades)
        lines.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS{af}[a{i}];")
        labels += [f"[v{i}]", f"[a{i}]"]
    lines.append("".join(labels) + f"concat=n={n}:v=1:a=1[cv][ca];")
    # HDR→SDR tonemap（仅 source 为 HLG/PQ 时非空）+ scale 串成一条 [cv]→[outv] vchain。
    # SDR 源 tonemap="" → 与改动前行为完全一致（scale_h 有则 scale、无则 null）。
    vchain = [c for c in (tonemap, f"scale=-2:{scale_h}" if scale_h else "") if c]
    lines.append(f"[cv]{','.join(vchain) if vchain else 'null'}[outv]")
    out_v = "[outv]"
    return "\n".join(lines), out_v


def validate_keep_ranges(keep: list[dict], total: float | None = None) -> None:
    """人审后的 EDL 是手改产物，送 ffmpeg 前校验：正时长、按 src_start 升序、无重叠、不越界。"""
    prev_end: float | None = None
    for i, kr in enumerate(keep):
        a, b = float(kr["src_start"]), float(kr["src_end"])
        # NaN 与所有比较均为 False，会静默钻过下面全部校验生成 trim=nan（Codex review 抓漏）
        if not (math.isfinite(a) and math.isfinite(b)):
            raise ValueError(f"keep_ranges[{i}] 含非有限数 [{a}, {b}]（NaN/Infinity 非法）")
        if a < 0 or b <= a:
            raise ValueError(f"keep_ranges[{i}] 非法区间 [{a}, {b}]（需 0 ≤ start < end）")
        if prev_end is not None and a < prev_end:
            raise ValueError(f"keep_ranges[{i}] 与前段重叠/乱序（start {a} < 前段 end {prev_end}）——EDL 需按时间升序且不重叠")
        if total is not None:
            # 0.05s 只是尾点的 container rounding 容忍；起点必须在片内，
            # 否则整段落在 EOF 后但宽度 < 50ms 的空区间会钻过（Codex review 抓漏）
            if a >= total:
                raise ValueError(f"keep_ranges[{i}] 起点越界（start {a} ≥ 源时长 {total:.3f}s）")
            if b > total + 0.05:
                raise ValueError(f"keep_ranges[{i}] 越界（end {b} > 源时长 {total:.3f}s）")
        prev_end = b


def rough_cut(out_dir: Path, project: str, scale_h: int | None = 1080,
              allow_unlocked: bool = False, log: Path | None = None,
              crossfade: float = 0.012) -> Path:
    out_dir = Path(out_dir)
    if not allow_unlocked:
        # require_lock 返回校验过的那份 EDL——用返回值而非二次读盘，消掉校验后换文件的窗口
        edl = require_lock(out_dir, project, "cut_lock")
        if not edl.get("human_locked"):
            raise RuntimeError("[lock] edl.human_locked=False——剪点未经人审锁定。`talkcut lock cut_lock` 会同步置位。")
    else:
        edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
    source = Path(edl["source_timeline"])
    keep = edl["keep_ranges"]
    if not keep:
        raise ValueError("EDL keep_ranges 为空")
    validate_keep_ranges(keep, float(probe_media(source)["duration_sec"]))

    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    tonemap = hdr_tonemap_chain(source)  # HDR→SDR：仅 HLG/PQ 源非空，SDR 零改动
    graph, out_v = build_filtergraph(keep, scale_h, crossfade, tonemap=tonemap)
    fg_file = work / "filtergraph_roughcut.txt"
    fg_file.write_text(graph, encoding="utf-8")

    rough = out_dir / "rough_cut.mp4"
    run_video_encode(lambda enc: [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-filter_complex_script", str(fg_file), "-map", out_v, "-map", "[ca]",
        *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(rough), "-y",
    ], log)

    # 回填 out_start
    acc = 0.0
    for kr in keep:
        kr["out_start"] = round(acc, 3)
        acc += float(kr["src_end"]) - float(kr["src_start"])
    (out_dir / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")

    mark_stage(out_dir, project, "rough_cut", "done", [str(rough)], verified=rough.exists(),
               note=f"{len(keep)} 段保留，输出 ~{acc:.1f}s{'；HDR→SDR tonemap' if tonemap else ''}")
    return rough
