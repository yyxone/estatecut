"""本地音乐库：扫描（子文件夹 = 分类）+ 按配置选曲（可复现，选择结果进报告）。

选曲去重（近 N 个视频窗口 LRU）+ 使用台账在本文件：按 source_video 折叠台账取末 N 个视频的
行组排除集（reprocess 刷行不挤窗）、池排空退化为最久未用（LRU）兜底、导出成功后 append 台账
（写在曲库项目 exports/，非曲目区，见 docs）。

favorites 池（P1-1，music.favorites 配置，默认关）：从选曲决策记录（selection_log.jsonl）
离线挖掘 Steven 的换曲偏好（explicit 换曲序列 = 唯一可信信号，使用频次不算偏好），
去重后的剩余池内做偏好加权 + 探索位（无信号新曲保底轮换）。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError

MUSIC_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}

# 跨进程排他锁（R5 L5）：并发 propcut run append 同一台账时，Windows 无锁并发
# append 会交错断行（tests/test_r5_ledger_sheet.py S8 实测复现）。锁打在 sidecar
# `<台账名>.lock` 固定第 0 字节上，与内容写入解耦。
if os.name == "nt":
    import msvcrt

    def _lock_handle(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # 最多重试 ~10s 后 OSError

    def _unlock_handle(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_handle(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock_handle(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class MusicChoice:
    """choose_music 结果 + 去重元信息（供 pipeline 写 run jsonl / 台账）。

    dedup_mode: none（未加音乐）| explicit（显式指定放行不去重）| disabled（window=0 或无台账）
                | normal（去重后池非空随机）| lru_fallback（池被排空，退化选最久未用）。
    """

    path: Path | None
    explicit: bool = False
    dedup_mode: str = "none"
    excluded_count: int = 0
    warning: str | None = None
    pool_size: int = 0  # 本次选曲候选池大小（explicit/none 为 0）；选曲决策记录用
    # favorites 加权（P1-1）：off（未启用）| no_data（启用但池内无偏好信号）
    # | explore（探索位命中，从无信号曲均匀选）| weighted（按偏好加权选出）。
    # explicit / lru_fallback 路径不参与（Steven 点名 / 确定性兜底），恒 off。
    favorites_mode: str = "off"
    pool: str | None = None
    license_scope: str | None = None
    commercial_ok: bool | None = None


def scan_library(library: Path) -> dict[str, list[Path]]:
    """{分类: [文件...]}；库根目录直属文件归 "" 分类。分类 = 一级子文件夹名。"""
    library = Path(library)
    result: dict[str, list[Path]] = {}
    for p in sorted(library.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in MUSIC_EXTENSIONS:
            continue
        rel = p.relative_to(library)
        category = rel.parts[0] if len(rel.parts) > 1 else ""
        result.setdefault(category, []).append(p)
    return result


def track_category(library: str | Path, track: str | Path) -> str:
    """曲子相对曲库的一级子文件夹名（= 分类）；库根直属或库外 → ""。"""
    try:
        rel = Path(track).resolve().relative_to(Path(library).resolve())
    except (ValueError, OSError):
        return ""
    return rel.parts[0] if len(rel.parts) > 1 else ""


def _resolve_str(value: Any) -> str:
    """归一为 resolve 后的绝对路径字符串（比对台账 track_path 用），失败退回原样。"""
    try:
        return str(Path(value).resolve())
    except (OSError, ValueError, TypeError):
        return str(value)


def _load_curated_pool(music_cfg: dict[str, Any]) -> tuple[list[Path], dict[str, bool]]:
    """加载精选曲池，并按 personal/commercial/user_selected 开关过滤。

    每首必须显式带 commercial_ok 布尔 mark。personal 全收；commercial 只收 true。
    user_selected 仅放行登记了使用决定且每曲都有 user_approved 的具名曲池。
    相对 file 按 music.library 解析，绝对路径用于未来 personal 曲目分架但不复制音频。
    返回 (曲目路径, resolve 路径 -> commercial_ok)。不写曲库或 DB。
    """
    pool_name = music_cfg.get("pool")
    pools_file = music_cfg.get("pools_file")
    if not pool_name or not pools_file:
        raise ConfigError("music.pool 已设置时 music.pools_file 必须解析为现有文件")
    try:
        with Path(pools_file).open("r", encoding="utf-8") as handle:
            doc = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"读取精选曲池失败: {pools_file}: {exc}") from exc
    pools = doc.get("pools") if isinstance(doc, dict) else None
    if not isinstance(pools, dict) or pool_name not in pools:
        available = sorted(pools) if isinstance(pools, dict) else []
        raise ConfigError(f"music.pool=`{pool_name}` 不存在，可用: {available}")
    definition = pools[pool_name]
    tracks = definition.get("tracks") if isinstance(definition, dict) else None
    if not isinstance(tracks, list) or not tracks:
        raise ConfigError(f"精选曲池 `{pool_name}` 缺少非空 tracks 列表")

    library = Path(music_cfg["library"])
    # 正式 workflow 经过 load_config()，默认值是 commercial；这里保留裸 dict
    # 调用的历史语义，避免旧调用方因未显式提供新字段而突然拒绝库外曲目。
    scope = music_cfg.get("license_scope", "personal")
    if scope == "user_selected" and definition.get("user_selected_allowed") is not True:
        raise ConfigError(f"精选曲池 `{pool_name}` 未登记 user_selected 使用决定")
    selected: list[Path] = []
    marks: dict[str, bool] = {}
    seen: set[str] = set()
    for index, item in enumerate(tracks, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"精选曲池 `{pool_name}` 第 {index} 项必须是 mapping")
        file_value = item.get("file")
        if not isinstance(file_value, str) or not file_value.strip():
            raise ConfigError(f"精选曲池 `{pool_name}` 第 {index} 项缺少非空 file")
        commercial_ok = item.get("commercial_ok")
        if scope == "user_selected" and item.get("taste_status") != "user_approved":
            raise ConfigError(f"user_selected 曲目必须有 taste_status: user_approved（第 {index} 项）")
        if not isinstance(commercial_ok, bool):
            raise ConfigError(
                f"精选曲池 `{pool_name}` 第 {index} 项必须显式标 commercial_ok: true/false")
        path = Path(file_value).expanduser()
        if not path.is_absolute():
            path = library / path
        path = path.resolve()
        if not path.is_file() and definition.get("library_db") and item.get("library_track_id"):
            # The owning library may have moved the track as part of review. Resolve the
            # same indexed content, never a name-based substitute or a rejected track.
            db_path = Path(definition["library_db"]).resolve()
            try:
                with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as con:
                    con.execute("PRAGMA query_only=ON")
                    row = con.execute(
                        "SELECT local_file_path,file_hash_sha256,status FROM tracks WHERE id=?",
                        (item["library_track_id"],),
                    ).fetchone()
                if not row or row[2] == "rejected" or not item.get("sha256") or row[1] != item["sha256"]:
                    raise ConfigError(f"曲库 ID/哈希/状态与认可曲不一致: {item['library_track_id']}")
                path = Path(row[0]).resolve()
                with path.open("rb") as audio:
                    actual_hash = hashlib.file_digest(audio, "sha256").hexdigest()
                if actual_hash != item["sha256"]:
                    raise ConfigError(f"曲库移位文件 SHA256 不符: {path}")
            except (sqlite3.Error, OSError) as exc:
                raise ConfigError(f"读取移位曲目失败: {db_path}: {exc}") from exc
        key = _resolve_str(path)
        if key in seen:
            raise ConfigError(f"精选曲池 `{pool_name}` 有重复曲目: {path}")
        seen.add(key)
        marks[key] = commercial_ok
        if scope == "commercial" and not commercial_ok:
            continue
        if not path.is_file():
            raise ConfigError(f"精选曲池 `{pool_name}` 的音乐文件不存在: {path}")
        if path.suffix.lower() not in MUSIC_EXTENSIONS:
            raise ConfigError(f"精选曲池 `{pool_name}` 的文件格式不支持: {path}")
        selected.append(path)
    if not selected:
        if scope == "commercial":
            if definition.get("user_selected_allowed") is True:
                raise ConfigError(
                    f"精选曲池 `{pool_name}` 是具名认可池，运行配置请设 license_scope: user_selected")
            raise ConfigError(f"精选曲池 `{pool_name}` 没有 commercial_ok=true 的曲目")
        raise ConfigError(f"精选曲池 `{pool_name}` 在 license_scope={scope} 下为空")
    return selected, marks


def _select_pool(catalog: dict[str, list[Path]],
                 music_cfg: dict[str, Any]) -> tuple[list[Path], dict[str, bool]]:
    """按精选池 / categories / category / random 组曲池（去重在 choose_music）。"""
    if music_cfg.get("pool"):
        return _load_curated_pool(music_cfg)

    # categories（来自 profile 或用户直接配）：有序 fallback，取第一个存在且非空的分类
    categories = music_cfg.get("categories")
    if categories:
        for category in categories:
            pool = catalog.get(category)
            if pool:
                return pool, {}
        raise ConfigError(
            f"music.categories {list(categories)} 在曲库中都不存在或为空，"
            f"可用分类: {sorted(k for k in catalog if k)}")

    if music_cfg.get("select") == "category":
        category = music_cfg["category"]
        if category not in catalog:
            raise ConfigError(f"音乐分类 `{category}` 不存在，可用: {sorted(k for k in catalog if k)}")
        return catalog[category], {}
    return [p for files in catalog.values() for p in files], {}  # random：全库


def _read_ledger_rows(ledger_path: Path,
                      label: str = "使用台账") -> tuple[list[dict[str, Any]], str | None]:
    """读 append-only JSONL（使用台账 / 选曲决策记录）→ (rows, warning)。

    文件缺失 → ([], None)（首跑正常，不告警）；坏行跳过不炸、汇总告警；读异常 → ([], warning)。
    """
    if not ledger_path.exists():
        return [], None
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"读取{label}失败，按空处理: {exc}"
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, (f"{label}有 {bad} 行损坏已跳过（按剩余行处理）" if bad else None)


def _last_used_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    """{resolve 后路径 或 文件名: 最近使用时间}（ISO 字典序 = 时序）。LRU 兜底用。"""
    last: dict[str, str] = {}
    for r in rows:
        at = str(r.get("at") or "")
        tp = r.get("track_path")
        tn = r.get("track_name")
        if tp:
            key = _resolve_str(tp)
            if at >= last.get(key, ""):
                last[key] = at
        if tn and at >= last.get(str(tn), ""):
            last[str(tn)] = at
    return last


def favorites_scores(ledger_path: Path) -> tuple[dict[str, int], str | None]:
    """从选曲决策记录（P0-4）离线挖掘偏好净分（P1-1 favorites 池）。

    信号语义（append_selection_log docstring 的约定）：按 (config, source_video) 折叠成
    换曲序列，末条 explicit=True → 该曲 +2（Steven 亲手点名，唯一可信正信号）+ 同序列
    此前的非 explicit 条 -1（被亲手换掉的负信号）。纯自动末条不计分——接受 ≠ 偏好，
    使用频次是随机选择的结果（伪偏好陷阱，见 suggest.py docstring）。
    返回 ({resolve路径 与 文件名 双键: 净分}, warning)；记录缺失 → ({}, None)。
    """
    rows, warn = _read_ledger_rows(selection_log_path(ledger_path), label="选曲决策记录")
    seqs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for idx, r in enumerate(rows):
        # 折叠键含 config：视频名高度模板化（客厅.mp4/卧室.mp4），跨 deal 必撞，只按
        # source_video 会把两个 deal 的编辑序列误并成一条、污染正负分（Codex P1 审）
        vid = str(r.get("source_video") or r.get("video") or f"__row_{idx}")
        seqs.setdefault((str(r.get("config") or ""), vid), []).append(r)

    scores: dict[str, int] = {}

    def bump(row: dict[str, Any], delta: int) -> None:
        tp, tn = row.get("selected_track"), row.get("selected_name")
        if tp:
            key = _resolve_str(tp)
            scores[key] = scores.get(key, 0) + delta
        if tn:
            scores[str(tn)] = scores.get(str(tn), 0) + delta

    for seq in seqs.values():
        if not seq[-1].get("explicit"):
            continue
        bump(seq[-1], 2)
        for prev in seq[:-1]:
            if not prev.get("explicit"):
                bump(prev, -1)
    return scores, warn


def _favorites_pick(pool: list[Path], rng: random.Random, music_cfg: dict[str, Any],
                    ledger_path: Path | None) -> tuple[Path, str, str | None]:
    """池内随机选曲，favorites 启用时按偏好加权（P1-1）。返回 (曲子, mode, warning)。

    探索位：exploration 概率从**无信号曲**里均匀选——保证新曲持续被听到、进入轮换
    （sonic identity 共识 = 小曲池轮换 + 有意识补新，不是永远同几首）；全池都有
    信号时无曲可探，直接加权。池内全无信号时不掷探索骰、退化为均匀随机
    （与未启用同一条 rng 消耗路径，行为 = 现状）。
    """
    fav = music_cfg.get("favorites") or {}
    if not fav.get("enabled") or ledger_path is None:
        return rng.choice(pool), "off", None
    scores, warn = favorites_scores(ledger_path)

    # 池内同名文件禁用 name 兜底键：否则一首的 filename 分会串给另一首（两个分类都有
    # same.mp3、select=random 全库摊平时可触发）——basename 池内唯一才允许兜底（Codex P1 审）
    name_counts: dict[str, int] = {}
    for t in pool:
        name_counts[t.name] = name_counts.get(t.name, 0) + 1
    dup_scored = sorted(n for n, c in name_counts.items() if c > 1 and scores.get(n))
    if dup_scored:
        warn = _merge_warnings(warn, f"池内重名文件禁用偏好 name 兜底键防串曲: {dup_scored[:3]}")

    def score_of(track: Path) -> int:
        s = scores.get(_resolve_str(track))
        if s is not None:
            return s
        return scores.get(track.name, 0) if name_counts[track.name] == 1 else 0

    if not any(score_of(t) for t in pool):
        return rng.choice(pool), "no_data", warn
    fresh = [t for t in pool if score_of(t) == 0]
    if fresh and rng.random() < float(fav.get("exploration", 0.25)):
        return rng.choice(fresh), "explore", warn
    boost = float(fav.get("boost", 3.0))
    weights = [boost if score_of(t) > 0 else (1.0 / boost if score_of(t) < 0 else 1.0)
               for t in pool]
    return rng.choices(pool, weights=weights, k=1)[0], "weighted", warn


def _merge_warnings(*warns: str | None) -> str | None:
    merged = "; ".join(w for w in warns if w)
    return merged or None


def choose_music(music_cfg: dict[str, Any], rng: random.Random,
                 override_music: str | None = None,
                 ledger_path: Path | None = None) -> MusicChoice:
    """按配置选一首 + 去重元信息；override_music（overrides 里指定）优先于配置的选择方式。

    去重：读 ledger_path 末 no_repeat_window 行组排除集（按 track_path resolve 比对、兜底 track_name），
    减完非空 → 池内随机（favorites 启用时按偏好加权 + 探索位，见 _favorites_pick）；
    减完为空 → LRU 兜底选最久未用（从未用过视为最旧，favorites 不参与）。
    显式指定（override_music 或 select=file）不去重；commercial 模式下仍必须可确认可商用。
    window=0 或 ledger_path=None → 不去重。
    去重是硬约束、favorites 是软偏好：加权只在去重减完后的剩余池内做，窗口永远赢。
    """
    # 正式 workflow 经过 load_config()，默认值是 commercial；这里保留裸 dict
    # 调用的历史语义，避免旧调用方因未显式提供新字段而突然拒绝库外曲目。
    scope = music_cfg.get("license_scope", "personal")
    pool_name = music_cfg.get("pool")
    if not music_cfg.get("enabled"):
        return MusicChoice(None, dedup_mode="none", pool=pool_name, license_scope=scope)
    if scope == "user_selected" and not pool_name:
        raise ConfigError("user_selected 仅用于用户明确认可的具名曲池")
    library = Path(music_cfg["library"])
    curated_pool: list[Path] | None = None
    commercial_marks: dict[str, bool] = {}
    if pool_name:
        curated_pool, commercial_marks = _load_curated_pool(music_cfg)

    def finish(choice: MusicChoice) -> MusicChoice:
        choice.pool = pool_name
        choice.license_scope = scope
        if choice.path is not None:
            mark = commercial_marks.get(_resolve_str(choice.path))
            if mark is None and scope == "commercial":
                try:
                    choice.path.resolve().relative_to(library.resolve())
                    mark = True  # 非精选池旧路径：library 的既有契约是商用货架
                except (ValueError, OSError):
                    pass
            choice.commercial_ok = mark
        return choice

    wanted = override_music or (music_cfg.get("file") if music_cfg.get("select") == "file" else None)
    if wanted:
        p = Path(wanted)
        if not p.is_absolute():
            p = library / p
        if not p.exists():
            raise ConfigError(f"指定的音乐文件不存在: {p}")
        warning = None
        inside_library = True
        try:
            p.resolve().relative_to(library.resolve())
        except (ValueError, OSError):
            inside_library = False
        mark = commercial_marks.get(_resolve_str(p))
        if scope == "user_selected" and mark is None:
            raise ConfigError(f"user_selected 指定曲必须属于已认可曲池: {p}")
        if scope == "commercial" and mark is False:
            raise ConfigError(f"commercial 模式禁止使用 commercial_ok=false 的指定曲: {p}")
        if scope == "commercial" and not inside_library and mark is not True:
            raise ConfigError(f"commercial 模式无法确认可商用（指定曲不在商用库且无 commercial_ok=true mark）: {p}")
        if not inside_library and mark is None:
            warning = f"显式指定曲不在配置曲库内（personal 模式放行）: {p}"
        return finish(MusicChoice(
            p, explicit=True, dedup_mode="explicit", warning=warning,
            commercial_ok=(True if inside_library and scope == "commercial" else mark),
        ))

    if curated_pool is not None:
        pool = curated_pool
    else:
        catalog = scan_library(library)
        if not catalog:
            raise ConfigError(f"音乐库为空（支持 {sorted(MUSIC_EXTENSIONS)}）: {library}")
        pool, commercial_marks = _select_pool(catalog, music_cfg)

    window = music_cfg.get("no_repeat_window", 30)
    window = int(window) if isinstance(window, (int, float)) and not isinstance(window, bool) else 0
    if ledger_path is None or window <= 0:
        track, fav_mode, fav_warn = _favorites_pick(pool, rng, music_cfg, ledger_path)
        return finish(MusicChoice(track, dedup_mode="disabled", pool_size=len(pool),
                                  favorites_mode=fav_mode, warning=fav_warn))

    rows, warning = _read_ledger_rows(ledger_path)
    # 去重窗口按"视频"不按"行"：按 source_video（回退 video）折叠，每视频取最后一行，
    # 取最近 window 个不同视频的行组（reprocess 同一视频刷行不挤窗）。
    last_row_by_video: dict[str, dict[str, Any]] = {}
    for idx, r in enumerate(rows):
        vid = str(r.get("source_video") or r.get("video") or f"__row_{idx}")
        if vid in last_row_by_video:
            del last_row_by_video[vid]  # 移到末尾：更新该视频"最近出现"位次
        last_row_by_video[vid] = r
    recent = list(last_row_by_video.values())[-window:]
    excluded_paths = {_resolve_str(r["track_path"]) for r in recent if r.get("track_path")}
    excluded_names = {str(r["track_name"]) for r in recent if r.get("track_name")}

    def is_recent(track: Path) -> bool:  # track_path 优先，兜底 track_name（库迁移后仍去重）
        return _resolve_str(track) in excluded_paths or track.name in excluded_names

    remaining = [t for t in pool if not is_recent(t)]
    excluded_count = len(pool) - len(remaining)
    if remaining:
        track, fav_mode, fav_warn = _favorites_pick(remaining, rng, music_cfg, ledger_path)
        return finish(MusicChoice(track, dedup_mode="normal",
                                  excluded_count=excluded_count,
                                  warning=_merge_warnings(warning, fav_warn),
                                  pool_size=len(pool), favorites_mode=fav_mode))

    # 池被排空 → LRU 兜底：全台账建最近使用时间表，选 pool 里最久未用（"" = 从未用 = 最旧）
    last_used = _last_used_map(rows)

    def used_at(track: Path) -> str:
        return last_used.get(_resolve_str(track)) or last_used.get(track.name) or ""

    chosen = min(pool, key=lambda t: (used_at(t), _resolve_str(t)))  # 稳定：同 time 按路径
    return finish(MusicChoice(chosen, dedup_mode="lru_fallback",
                              excluded_count=excluded_count, warning=warning,
                              pool_size=len(pool)))


def _append_jsonl(path: Path, row: dict[str, Any], label: str) -> str | None:
    """append 一行 JSONL（父目录缺则建），持跨进程排他锁写入 + flush/fsync。

    写失败（含 ~10s 内抢不到锁）返回 warning（不炸管线）；成功返回 None。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with open(path.with_name(path.name + ".lock"), "ab") as lock_handle:
            _lock_handle(lock_handle)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                _unlock_handle(lock_handle)
    except OSError as exc:
        return f"写{label}失败（不影响成片）: {exc}"
    return None


def append_usage_ledger(ledger_path: Path, row: dict[str, Any]) -> str | None:
    """append 一行到使用台账（曲库项目 exports/ 的 append-only 例外；父目录缺则建）。

    写失败返回 warning（不炸管线）；成功返回 None。utf-8 + ensure_ascii=False。
    """
    return _append_jsonl(ledger_path, row, "使用台账")


def selection_log_path(ledger_path: Path) -> Path:
    """选曲决策记录文件：与使用台账同目录（曲库项目 exports/ 同一 append-only 例外）。"""
    return ledger_path.parent / "selection_log.jsonl"


def append_selection_log(ledger_path: Path, row: dict[str, Any]) -> str | None:
    """append 一行选曲决策记录（P0-4）。

    与使用台账的分工：台账 = 去重窗口 + 用量事实；本记录 = 决策上下文
    （profile / categories / 池大小 / dedup_mode），供 favorites 池（P1-1）离线挖掘。
    同一视频多条记录 = 换曲重导序列，末条为最终选择，被 explicit 替换的前几条是负信号。
    candidates[] / rejected[] 字段留给推荐器（P0-1）与 audition（P0-2）接入时填充。
    写失败返回 warning（不炸管线）。
    """
    return _append_jsonl(selection_log_path(ledger_path), row, "选曲决策记录")
