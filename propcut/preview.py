"""preview 子命令 — 对每条视频、每个预设各出一帧对比图 + 一张网格拼图，供挑预设。

内部核对图（非对外产物）：取样 1 帧（默认 = 检测起点 +3s，无检测缓存则视频 25% 处），
对每个预设过 correction(HDR tonemap 若适用) + style 链渲一帧 JPG，再 PIL 拼网格 + 标预设名。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from estatecut.ffmpeg_tools import probe_media, run_command
from estatecut.utils import ensure_dir, require_outside

from . import grading
from .config import load_config
from .grading import load_presets
from .pipeline import _load_detection_cache, _slug, scan_videos

# 预览帧缩到最长边 720（真彩代表性够，省时省盘；非对外成片）
_PREVIEW_SCALE = "scale='min(720,iw)':-2"


def _pick_time(cache: dict[str, Any] | None, duration: float, time_sec: float | None) -> float:
    if time_sec is not None:
        return max(0.0, min(time_sec, duration - 0.1))
    if cache and "detection" in cache:
        t = float(cache["detection"].get("detected_start_time", 0.0)) + 3.0
        return max(0.0, min(t, duration - 0.1))
    return max(0.0, duration * 0.25)


def _render_frame(src: Path, t: float, correction: str, style: str, dest: Path) -> None:
    vf = ",".join(p for p in (correction, style, _PREVIEW_SCALE) if p)
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{t:.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", vf, "-q:v", "3", str(dest),
    ])


def _grid(frames: list[tuple[str, Path]], dest: Path) -> None:
    """PIL 拼网格 + 每格左上角标预设名（默认字体，内部核对够用）。"""
    imgs = [(name, Image.open(p).convert("RGB")) for name, p in frames if p.exists()]
    if not imgs:
        return
    cols = min(4, int(math.ceil(math.sqrt(len(imgs)))))
    rows = int(math.ceil(len(imgs) / cols))
    cell_w = max(im.width for _, im in imgs)
    cell_h = max(im.height for _, im in imgs)
    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    for i, (name, im) in enumerate(imgs):
        x, y = (i % cols) * cell_w, (i // cols) * cell_h
        canvas.paste(im, (x, y))
        draw.rectangle([x + 2, y + 2, x + 8 + 7 * len(name), y + 16], fill=(0, 0, 0))
        draw.text((x + 5, y + 4), name, fill=(255, 255, 255))
    canvas.save(dest, quality=88)


def run_preview(config_path: Path, only: str | None = None, time_sec: float | None = None) -> list[dict[str, Any]]:
    cfg = load_config(Path(config_path))
    input_dir = Path(cfg["input"]["dir"])
    output_dir = Path(cfg["output"]["dir"])
    require_outside(output_dir, input_dir)  # 硬约束：绝不写进源媒体目录树
    reports_dir = ensure_dir(output_dir / "_reports")

    gr = cfg["grading"]
    custom_pf = Path(gr["presets_file"]) if gr.get("presets_file") else None
    presets = load_presets(custom_pf)

    videos = scan_videos(cfg)
    if only:
        videos = [v for v in videos if only.lower() in v.name.lower()]
    if not videos:
        print(f"[propcut:preview] 没有找到匹配的视频（input={input_dir}, only={only!r}）")
        return []

    results: list[dict[str, Any]] = []
    for src in videos:
        rel = src.relative_to(input_dir)
        slug = _slug(rel)
        duration = float(probe_media(src).get("duration_sec") or 0)
        if duration <= 0:
            print(f"[propcut:preview] [FAIL] {rel} — ffprobe 读不到时长")
            results.append({"video": str(src), "slug": slug, "error": "no duration"})
            continue
        color_info = grading.probe_color_info(src)
        correction = grading.build_correction_chain(color_info, gr)  # HDR 源所见即所得
        cache = _load_detection_cache(reports_dir, slug)
        t = _pick_time(cache, duration, time_sec)

        frames: list[tuple[str, Path]] = []
        for name in presets:
            dest = reports_dir / f"{slug}_preview_{name}.jpg"
            try:
                # build_style_chain 也放进 try：dlog 预设缺 LUT 会抛 GradingError，
                # 该预设跳过即可，不拖垮其余预设与整张网格
                style = grading.build_style_chain({"mode": "preset", "preset": name}, presets)
                _render_frame(src, t, correction, style, dest)
                frames.append((name, dest))
            except Exception as exc:  # noqa: BLE001 — 单预设失败不拖垮整张网格
                print(f"[propcut:preview] [warn] {rel} 预设 {name} 跳过: {exc}")
        grid_path = reports_dir / f"{slug}_preview_grid.jpg"
        _grid(frames, grid_path)
        print(f"[propcut:preview] [ok] {rel} — {len(frames)} 预设 @ {t:.1f}s → {grid_path.name}")
        results.append({"video": str(src), "slug": slug, "time": round(t, 2),
                        "presets": [n for n, _ in frames], "grid": str(grid_path),
                        "hdr_tonemapped": bool(correction)})
    return results
