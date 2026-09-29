"""propcut 选曲推荐器（P0-1）——曲库 DB 只读打分，出 top-N 清单。

只做建议：不动配置、不写 DB、不写台账（决策记录在 Steven 采纳后由 process/stitch
导出链写，见 music.append_selection_log）。曲库 app 层（search.py/db.py）有 DDL /
建目录副作用，这里**不 import**，直接 sqlite3 只读连接（URI mode=ro + PRAGMA
query_only=ON 双保险）。

过滤闸 fail closed（任一不满足即排除，绝不放宽）：
  分类模式要求 status='approved' AND usage_tier='commercial'；精选池模式先按
  personal/commercial mark 过滤。两者都要求有 track_analysis 行、vocals_p 非 NULL 且
  <= 阈值、red_flags 为空、文件存在。精选池可按显式 mark 引用 library 外 personal 曲。

打分（0-1 加权和）：BPM 契合 0.40（半速/倍速等价）+ 曲长契合 0.35（曲长 >= 片长满分，
不足按比例衰减——循环有接缝罚）+ energy(RMS 池内百分位) 0.25 弱权重。
不用使用频次加权（频次 = 随机选择的结果，不是 Steven 偏好——伪偏好陷阱）。
每艺术家上限 2 首（top-N 输出层去重）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from estatecut.resources_path import resource_path

from .config import load_config
from .music import _load_curated_pool

W_BPM, W_DURATION, W_ENERGY = 0.40, 0.35, 0.25
NEUTRAL = 0.5            # selection 缺该维度时的中性分
VOCALS_MAX_DEFAULT = 0.5  # vocals_p 硬闸默认阈值（房源片纯器乐诉求）
ENERGY_TARGET_RANK = {"low": 0.25, "medium": 0.5, "high": 0.75}
MAX_PER_ARTIST = 2


class SuggestUnavailable(RuntimeError):
    """曲库 DB 不可用 → 推荐器降级（现行随机选曲不受影响），CLI 提示后 exit 2。"""


def resolve_library_db(library: Path) -> Path | None:
    """标准布局 .../music/01_approved/<tier> → <库根>/db/music_library.sqlite；推导不出 → None。"""
    parts = Path(library).parts
    if len(parts) >= 3 and parts[-3] == "music" and parts[-2] == "01_approved":
        return Path(library).parents[2] / "db" / "music_library.sqlite"
    return None


def load_selection_meta(profiles_file: Path, profile_name: str | None) -> dict[str, Any]:
    """读 profile 的 selection 节（推荐器元数据，运行配置里没有）。缺省 → {}。"""
    if not profile_name or not Path(profiles_file).is_file():
        return {}
    with Path(profiles_file).open("r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    profile = ((doc.get("profiles") or {}).get(profile_name)) or {}
    sel = profile.get("selection")
    return sel if isinstance(sel, dict) else {}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """只读连接：URI mode=ro + PRAGMA query_only=ON 双保险（本工具对曲库 DB 零写入）。"""
    if not db_path.is_file():
        raise SuggestUnavailable(f"曲库 DB 不存在: {db_path}")
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con
    except sqlite3.Error as exc:
        raise SuggestUnavailable(f"曲库 DB 打不开: {db_path} ({exc})") from exc


def fetch_candidates(db_path: Path, categories: list[str], library: Path,
                     vocals_max: float = VOCALS_MAX_DEFAULT) -> list[dict[str, Any]]:
    """过滤闸后的候选（含来源分类），按 categories 全部合并（fallback 语义由调用方决定）。"""
    con = _connect_ro(db_path)
    try:
        placeholders = ",".join("?" for _ in categories)
        rows = con.execute(
            f"""SELECT t.id, t.title, t.artist, t.category, t.local_file_path,
                       t.duration_seconds,
                       COALESCE(a.bpm, t.bpm) AS bpm, a.rms_energy, a.vocals_p, a.red_flags
                FROM tracks t
                JOIN track_analysis a ON a.track_id = t.id     -- 未分析 = 排除（fail closed）
                WHERE t.status = 'approved' AND t.usage_tier = 'commercial'
                  AND t.category IN ({placeholders})
                  AND a.vocals_p IS NOT NULL AND a.vocals_p <= ?""",
            [*categories, vocals_max]).fetchall()
    finally:
        con.close()

    lib_resolved = Path(library).resolve()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            flags = json.loads(r["red_flags"]) if r["red_flags"] else []
        except json.JSONDecodeError:
            flags = ["<unparseable>"]  # 解析不了当有 flag 处理（fail closed）
        if flags:
            continue
        p = Path(r["local_file_path"] or "")
        if not p.is_file():
            continue
        try:  # 必须在配置曲库树内，保证推荐结果可直接用于 override music
            p.resolve().relative_to(lib_resolved)
        except (ValueError, OSError):
            continue
        out.append({"id": r["id"], "title": r["title"], "artist": r["artist"],
                    "category": r["category"], "path": str(p),
                    "duration": float(r["duration_seconds"] or 0.0),
                    "bpm": float(r["bpm"]) if r["bpm"] else None,
                    "rms": float(r["rms_energy"]) if r["rms_energy"] is not None else None})
    return out


def fetch_pool_candidates(db_path: Path, paths: list[Path], commercial: bool,
                          vocals_max: float = VOCALS_MAX_DEFAULT, *,
                          user_selected: bool = False) -> list[dict[str, Any]]:
    """按精选池精确路径取候选；commercial 模式再叠 DB approved/commercial 防线。"""
    allowed = {str(Path(p).resolve()).casefold() for p in paths}
    con = _connect_ro(db_path)
    try:
        rights_gate = "AND t.status='approved' AND t.usage_tier='commercial'" if commercial else ""
        audio_gate = "1=1" if user_selected else "a.vocals_p IS NOT NULL AND a.vocals_p <= ?"
        rows = con.execute(
            f"""SELECT t.id, t.title, t.artist, t.category, t.local_file_path,
                       t.duration_seconds,
                       COALESCE(a.bpm, t.bpm) AS bpm, a.rms_energy, a.vocals_p, a.red_flags
                FROM tracks t LEFT JOIN track_analysis a ON a.track_id=t.id
                WHERE {audio_gate} {rights_gate}""",
            [] if user_selected else [vocals_max],
        ).fetchall()
    finally:
        con.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            flags = json.loads(r["red_flags"]) if r["red_flags"] else []
        except json.JSONDecodeError:
            flags = ["<unparseable>"]
        if flags and not user_selected:
            continue
        path = Path(r["local_file_path"] or "")
        if not path.is_file() or str(path.resolve()).casefold() not in allowed:
            continue
        out.append({
            "id": r["id"], "title": r["title"], "artist": r["artist"],
            "category": r["category"], "path": str(path),
            "audio_hints": {"red_flags": flags, "vocals_p": r["vocals_p"]},
            "duration": float(r["duration_seconds"] or 0.0),
            "bpm": float(r["bpm"]) if r["bpm"] else None,
            "rms": float(r["rms_energy"]) if r["rms_energy"] is not None else None,
        })
    return out


def _bpm_score(bpm: float | None, bpm_range: list[float] | None) -> float:
    """半速/倍速等价（61.5 视同 123）；落区间 = 1.0，区间外按相对距离衰减。"""
    if not bpm_range:
        return NEUTRAL
    if not bpm or bpm <= 0:
        return 0.0  # 有目标区间但曲子无 BPM 数据 → 无法证明契合（fail closed 不给中性分）
    lo, hi = float(bpm_range[0]), float(bpm_range[1])
    center = (lo + hi) / 2
    best = 0.0
    for b in (bpm, bpm * 2, bpm / 2):
        if lo <= b <= hi:
            return 1.0
        dist = (lo - b) if b < lo else (b - hi)
        best = max(best, 1.0 - dist / center)
    return max(0.0, best)


def _duration_score(track_dur: float, target_dur: float) -> float:
    """曲长 >= 片长 → 1.0（无循环接缝）；不足按比例衰减再罚 0.8（循环即有缝）。"""
    if target_dur <= 0:
        return NEUTRAL
    if track_dur >= target_dur:
        return 1.0
    return max(0.0, track_dur / target_dur) * 0.8


def _energy_scores(candidates: list[dict[str, Any]], energy: str | None) -> dict[int, float]:
    """池内 RMS 百分位 vs 目标位（low=0.25/medium=0.5/high=0.75）；无 energy → 全中性。"""
    target = ENERGY_TARGET_RANK.get(energy or "")
    if target is None:
        return {c["id"]: NEUTRAL for c in candidates}
    with_rms = sorted((c for c in candidates if c["rms"] is not None), key=lambda c: c["rms"])
    n = len(with_rms)
    scores = {c["id"]: 0.0 for c in candidates if c["rms"] is None}  # 有目标但无数据 → 0
    for i, c in enumerate(with_rms):
        rank = (i + 0.5) / n if n else 0.5
        scores[c["id"]] = max(0.0, 1.0 - abs(rank - target) * 2)
    return scores


def rank_candidates(candidates: list[dict[str, Any]], target_dur: float,
                    selection: dict[str, Any], top: int = 5) -> list[dict[str, Any]]:
    """打分排序 + 每艺术家上限；返回带 score/parts 的 top-N。"""
    bpm_range = selection.get("bpm_range")
    energy_by_id = _energy_scores(candidates, selection.get("energy"))
    scored = []
    for c in candidates:
        parts = {"bpm": round(_bpm_score(c["bpm"], bpm_range), 3),
                 "duration": round(_duration_score(c["duration"], target_dur), 3),
                 "energy": round(energy_by_id[c["id"]], 3)}
        score = W_BPM * parts["bpm"] + W_DURATION * parts["duration"] + W_ENERGY * parts["energy"]
        scored.append({**c, "score": round(score, 4), "parts": parts})
    scored.sort(key=lambda c: (-c["score"], c["path"]))  # 稳定：同分按路径

    picked: list[dict[str, Any]] = []
    per_artist: dict[str, int] = {}
    for c in scored:
        artist = (c["artist"] or "").strip().lower()
        if per_artist.get(artist, 0) >= MAX_PER_ARTIST:
            continue
        per_artist[artist] = per_artist.get(artist, 0) + 1
        picked.append(c)
        if len(picked) >= top:
            break
    return picked


def suggest(config_path: Path, duration: float, top: int = 5,
            strict_fallback: bool = False,
            vocals_max: float = VOCALS_MAX_DEFAULT) -> dict[str, Any]:
    """主入口：读 propcut 配置 → 曲库 DB 只读打分 → top-N 清单（纯查询，零写入）。"""
    cfg = load_config(Path(config_path))
    mus = cfg["music"]
    if not mus.get("enabled"):
        raise SuggestUnavailable("该配置 music.enabled=false，无曲可荐")
    library = Path(mus["library"])
    db_path = resolve_library_db(library)
    if db_path is None:
        raise SuggestUnavailable(
            f"music.library 非标准布局（.../music/01_approved/<tier>），无法定位曲库 DB: {library}")

    pool_name = mus.get("pool")
    categories = list(mus.get("categories") or [])
    if not pool_name and not categories:
        if mus.get("select") == "category" and mus.get("category"):
            categories = [mus["category"]]
        else:  # random 全库：用 DB 里 approved 商用类目全集
            con = _connect_ro(db_path)
            try:
                categories = [r[0] for r in con.execute(
                    "SELECT DISTINCT category FROM tracks "
                    "WHERE status='approved' AND usage_tier='commercial'")]
            finally:
                con.close()

    pf = mus.get("profiles_file")
    if pf:  # 相对路径相对配置文件所在目录（与 config._apply_music_profile 同语义）
        profiles_file = Path(pf).expanduser()
        if not profiles_file.is_absolute():
            profiles_file = Path(config_path).resolve().parent / profiles_file
    else:
        profiles_file = resource_path("music_profiles.yaml")
    selection = load_selection_meta(profiles_file, mus.get("profile"))

    if pool_name:
        pool_paths, _ = _load_curated_pool(mus)
        candidates = fetch_pool_candidates(
            db_path, pool_paths, mus.get("license_scope") == "commercial", vocals_max,
            user_selected=mus.get("license_scope") == "user_selected")
        categories = sorted({str(c["category"] or "") for c in candidates})
    elif strict_fallback:  # 现行运行时语义：第一个在 DB 中有曲的分类
        for cat in categories:
            cands = fetch_candidates(db_path, [cat], library, vocals_max)
            if cands:
                candidates = cands
                categories = [cat]
                break
        else:
            candidates = []
    else:  # 默认合并全部 categories 为候选池（来源分类在每条的 category 字段）
        candidates = fetch_candidates(db_path, categories, library, vocals_max)

    ranked = rank_candidates(candidates, duration, selection, top)
    return {"config": str(config_path), "profile": mus.get("profile"),
            "pool": pool_name, "license_scope": mus.get("license_scope"),
            "categories": categories, "strict_fallback": strict_fallback,
            "target_duration": duration, "selection": selection,
            "pool_size": len(candidates), "suggestions": ranked}


def format_report(result: dict[str, Any]) -> str:
    """人读清单（含 override 用法提示）。"""
    lines = [
        f"曲库候选 {result['pool_size']} 首（profile={result['profile'] or '-'}，"
        f"pool={result.get('pool') or '-'}，scope={result.get('license_scope') or '-'}，"
        f"分类={'/'.join(result['categories'])}，目标片长 {result['target_duration']:.0f}s，"
        f"selection={result['selection'] or '无（曲长为主）'}）",
        "",
        f"{'#':<2} {'总分':<6} {'BPM分':<5} {'长度分':<5} {'能量分':<5} 曲目",
    ]
    for i, s in enumerate(result["suggestions"], 1):
        bpm = f"{s['bpm']:.0f}" if s["bpm"] else "?"
        lines.append(
            f"{i:<2} {s['score']:<6.3f} {s['parts']['bpm']:<5.2f} {s['parts']['duration']:<5.2f} "
            f"{s['parts']['energy']:<5.2f} {s['title']} — {s['artist']} "
            f"[{s['category']}] {bpm}BPM {s['duration']:.0f}s")
        lines.append(f"   {s['path']}")
        hints = s.get("audio_hints", {})
        if hints.get("red_flags") or (hints.get("vocals_p") or 0) >= VOCALS_MAX_DEFAULT:
            lines.append(f"   机器参考（不否决用户认可）：{hints}")
    lines += ["", "采纳方式：overrides 里该视频加 music: \"<上面的路径>\"（explicit 通道，"
              "不去重）；或先跑 audition 小样试听再定。推荐只是清单，process 不受影响。"]
    return "\n".join(lines)
