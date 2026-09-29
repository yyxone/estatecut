"""§6 subtitle — 词级时间戳映射到剪后时间轴 + ASR 纠错 → 简体 UTF-8 SRT。

默认不烧录（单独 .srt 文件）。移植自 地下室如何看房 make_srt。
"""
from __future__ import annotations

import json
from pathlib import Path

from .state import mark_stage, require_lock

from estatecut.resources_path import resource_path
CONFIG_CORRECTIONS = resource_path("corrections.json")
MAX_CUE_CHARS = 14
MAX_CUE_GAP = 0.5
# 句末标点：cue 在这些字符处强制收束（标点优先于字符数硬切，避免切在句中）。
# 借鉴 MoneyPrinterTurbo 按标点断句思路——中文 word-level 时间轴直接照搬会碎成单字。
SENTENCE_END = "。！？；…!?;"


def _load_corrections() -> dict:
    if CONFIG_CORRECTIONS.exists():
        return json.loads(CONFIG_CORRECTIONS.read_text(encoding="utf-8")).get("replacements", {})
    return {}


def _fix(text: str, repl: dict) -> str:
    for a, b in repl.items():
        text = text.replace(a, b)
    return text


def _srt_ts(t: float) -> str:
    # 整体转毫秒再逐级 divmod——旧实现毫秒四舍五入后不进位（59.9996 → 00:00:60,000 非法时间戳）
    total_ms = max(0, int(round(t * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _flatten_words(transcript: dict) -> list[dict]:
    words = []
    for seg in transcript.get("segments", []):
        if seg.get("words"):
            words.extend(seg["words"])
        elif seg.get("text"):  # 无词级时退化到段级
            words.append({"start": seg["start"], "end": seg["end"], "word": seg["text"]})
    return words


def make_srt(out_dir: Path, project: str, allow_unlocked: bool = False) -> Path:
    out_dir = Path(out_dir)
    if not allow_unlocked:
        # 字幕同时消费 EDL（剪后时间轴）和 transcript（词级文本）——两份都要过人审锁；
        # require_lock 返回校验过的那份内容，此处不再二次读盘（TOCTOU）
        edl = require_lock(out_dir, project, "cut_lock")
        if not edl.get("human_locked"):
            raise RuntimeError("[lock] edl.human_locked=False——剪点未经人审锁定。`talkcut lock cut_lock` 会同步置位。")
        transcript = require_lock(out_dir, project, "transcript_lock")
    else:
        transcript = json.loads((out_dir / "transcript.json").read_text(encoding="utf-8"))
        edl = json.loads((out_dir / "edl.json").read_text(encoding="utf-8"))
    repl = _load_corrections()

    # keep 区间 + 输出偏移：偏移一律按 keep 时长累计重算。
    # out_start 是 rough_cut 回填的派生字段、不在 cut_lock 锁面内——直接信任它
    # 等于给了绕锁改字幕时间轴的口子，存了就必须与重算值一致
    keep = []
    acc = 0.0
    for kr in edl["keep_ranges"]:
        a, b = float(kr["src_start"]), float(kr["src_end"])
        if "out_start" in kr:
            stored = float(kr["out_start"])
            if abs(stored - acc) > 0.05:
                raise RuntimeError(
                    f"[subtitle] keep_ranges 的 out_start={stored} 与按剪单重算的 {acc:.3f} 偏差超 0.05s——"
                    f"该字段是机器回填值，被手改/错填会让字幕整体错位。重跑 rough-cut 回填或删掉该字段"
                )
        keep.append((a, b, acc))
        acc += b - a

    def to_out(t: float):
        for a, b, o in keep:
            if a <= t <= b:
                return o + (t - a)
        return None

    mapped = []
    for w in _flatten_words(transcript):
        mid = (float(w["start"]) + float(w["end"])) / 2
        os_ = to_out(mid)
        if os_ is None:
            continue
        mapped.append((os_, os_ + (float(w["end"]) - float(w["start"])), w["word"].strip()))
    mapped.sort()

    # 分句成 cue：句末标点优先收束 > 间隔 > 字符数上限
    cues, cur = [], []
    for os_, oe_, t in mapped:
        if oe_ <= os_:  # 0/负时长 word 跳过（防生成 00:00:00,000 --> 00:00:00,000 空时间轴 cue）
            continue
        # 纯标点 word（独立的句末/停顿标点）并入当前 cue 末尾，不孤立成单字幕
        if cur and t and all(c in SENTENCE_END + "，、,…" for c in t):
            cur.append((os_, oe_, t))
            cues.append(cur); cur = []
            continue
        if cur and (os_ - cur[-1][1] > MAX_CUE_GAP or sum(len(x[2]) for x in cur) >= MAX_CUE_CHARS):
            cues.append(cur); cur = []
        cur.append((os_, oe_, t))
        if t and t[-1] in SENTENCE_END:  # 句末标点 → 当场收一个完整 cue，不切在句中
            cues.append(cur); cur = []
    if cur:
        cues.append(cur)

    lines = []
    idx = 1
    for c in cues:
        start_t, end_t = c[0][0], c[-1][1]
        txt = _fix("".join(x[2] for x in c), repl).strip()
        if end_t <= start_t or not txt:  # 0 时长 / 空文本 cue 不写（剪辑软件无法导入空时间轴）
            continue
        lines += [str(idx), f"{_srt_ts(start_t)} --> {_srt_ts(end_t)}", txt, ""]
        idx += 1
    srt = out_dir / "字幕.srt"
    srt.write_text("\n".join(lines), encoding="utf-8")
    mark_stage(out_dir, project, "subtitle", "done", [str(srt)], verified=srt.exists(),
               note=f"{idx - 1} cue（简体/未烧录）")
    return srt
