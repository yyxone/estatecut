"""propcut CLI — detect / process / reprocess / preview / stitch 五模式。

用法:
    python -m propcut detect    --config configs/propcut.yaml
    python -m propcut process   --config configs/propcut.yaml [--force-detect] [--only 001]
    python -m propcut reprocess --config configs/propcut.yaml [--only 001]
    python -m propcut preview   --config configs/propcut.yaml [--only 001] [--time 5]
    python -m propcut stitch    --config configs/propcut.yaml   # EDL 在配置 stitch 节
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import run


def main(argv: list[str] | None = None) -> int:
    # Windows 默认 cp1252/GBK 控制台下中文帮助/报错会 UnicodeEncodeError——强制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="propcut", description="房产视频批量处理：智能裁头 + 调色 + 配乐 + 导出")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode, help_text in (
        ("detect", "只分析开头 + 输出检测结果与关键帧，不导出"),
        ("process", "完整处理（有检测缓存则复用）"),
        ("reprocess", "用已有检测结果 + overrides 重新导出，不重分析"),
        ("preview", "对每条视频每个预设各出一帧对比图 + 网格拼图，挑预设用"),
    ):
        p = sub.add_parser(mode, help=help_text)
        p.add_argument("--config", required=True, help="propcut YAML 配置文件路径")
        p.add_argument("--only", default=None, help="只处理文件名包含该子串的视频")
        if mode == "process":
            p.add_argument("--force-detect", action="store_true", help="忽略检测缓存强制重新分析")
        if mode == "preview":
            p.add_argument("--time", type=float, default=None, help="取样时间点（秒）；缺省 = 检测起点+3s 或 25% 处")

    p = sub.add_parser("stitch", help="按配置 stitch 节的 EDL 把多个片段拼成一条成片（整条配一首音乐）")
    p.add_argument("--config", required=True, help="propcut YAML 配置文件路径")

    p = sub.add_parser("audition", help="配乐小样：1 条视频 × N 候选曲等响度试听组（画面代理只编一次）")
    p.add_argument("--config", required=True, help="propcut YAML 配置文件路径")
    p.add_argument("--only", default=None, help="只处理文件名包含该子串的视频（必须收窄到恰好 1 条）")
    p.add_argument("--duration", type=float, default=18.0, help="小样长度秒数（默认 18，从正式开头起）")
    p.add_argument("--top", type=int, default=5, help="无 --tracks 时用推荐器 top-N 当候选（默认 5）")
    p.add_argument("--tracks", nargs="*", default=None, help="显式候选曲路径列表（给了就不走推荐器）")

    p = sub.add_parser("suggest", help="选曲推荐：曲库 DB 只读打分出 top-N 清单（纯建议，不动配置不写入）")
    p.add_argument("--config", required=True, help="propcut YAML 配置文件路径")
    p.add_argument("--duration", type=float, required=True, help="目标片长（秒），曲长契合打分用")
    p.add_argument("--top", type=int, default=5, help="出几条（默认 5）")
    p.add_argument("--strict-fallback", action="store_true",
                   help="按运行时语义只用第一个非空分类（默认合并 profile 全部 categories）")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    args = parser.parse_args(argv)
    if args.mode == "audition":
        from .audition import run_audition
        try:
            manifest = run_audition(Path(args.config), only=args.only, tracks=args.tracks,
                                    top=args.top, duration=args.duration)
        except ValueError as exc:
            print(f"audition 失败：{exc}")
            return 1
        for w in manifest["warnings"]:
            print(f"⚠ {w}")
        print(f"小样 {len(manifest['candidates'])} 个（{manifest['sample_duration']:.0f}s，"
              f"起点 {manifest['start']:.1f}s，已等响度对齐）：")
        for c in manifest["candidates"]:
            mark = "" if c["ok"] else "  ⚠ 产物校验失败"
            score = f"  推荐分 {c['suggest_score']:.3f}" if "suggest_score" in c else ""
            print(f"  {c['idx']:02d}. {c['track_name']}  (增益 {c['gain_db']:+.1f}dB{score}){mark}")
            print(f"      {c['output']}")
        bad = [c for c in manifest["candidates"] if not c["ok"]]
        return 1 if bad else 0
    if args.mode == "suggest":
        import json as _json
        from .suggest import SuggestUnavailable, format_report, suggest
        try:
            result = suggest(Path(args.config), duration=args.duration, top=args.top,
                             strict_fallback=args.strict_fallback)
        except SuggestUnavailable as exc:
            print(f"推荐器降级：{exc}\n（不影响 process 现行随机选曲，可直接照常出片）")
            return 2
        print(_json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else format_report(result))
        return 0
    if args.mode == "stitch":
        from .stitch import run_stitch
        entry = run_stitch(Path(args.config))
        return 0 if entry.get("success") else 1
    if args.mode == "preview":
        from .preview import run_preview
        run_preview(Path(args.config), only=args.only, time_sec=args.time)
        return 0
    results = run(Path(args.config), mode=args.mode, only=args.only,
                  force_detect=getattr(args, "force_detect", False))
    failed = [e for e in results if not e.get("success")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
