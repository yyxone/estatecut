"""propcut favorites 池 + 探索位（P1-1）单测 — 全 mock、tmp_path，不跑真 ffmpeg。

覆盖：config 校验（默认关 / 合并 / 白名单 / 范围）/ favorites_scores 序列语义
（explicit 末条 +2、被换掉的自动选 -1、纯自动不计分、name 兜底键）/
choose_music 四态（off / no_data / explore / weighted）/ 关闭与无数据时行为=现状
（同 seed 同曲）/ 去重硬约束压过偏好 / explicit 与 LRU 不参与 / 警告合并。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
import yaml

from propcut.config import ConfigError, load_config
from propcut.music import (append_selection_log, choose_music, favorites_scores,
                           selection_log_path)


def _track(lib: Path, category: str, name: str) -> Path:
    (lib / category).mkdir(parents=True, exist_ok=True)
    p = lib / category / name
    p.write_bytes(b"x")
    return p


def _sel_row(track: Path, *, source_video: str, explicit: bool) -> dict:
    return {"at": "2026-07-17T00:00:00+00:00", "pipeline": "propcut",
            "video": source_video.replace(".mp4", "_cut.mp4"), "source_video": source_video,
            "config": "c.yaml", "profile": None, "categories": None, "dedup_mode": "normal",
            "explicit": explicit, "pool_size": 2, "excluded_count": 0,
            "selected_track": str(track.resolve()), "selected_name": track.name,
            "selected_category": "calm"}


def _usage_row(track: Path, at: str, *, source_video: str) -> dict:
    return {"at": at, "pipeline": "propcut", "video": "out.mp4", "source_video": source_video,
            "track_path": str(track.resolve()), "track_name": track.name,
            "category": "calm", "explicit": False, "config": "c.yaml"}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def _fav_cfg(lib: Path, *, boost: float = 100.0, exploration: float = 0.0,
             window: int = 30) -> dict:
    return {"enabled": True, "library": str(lib), "select": "category", "category": "calm",
            "no_repeat_window": window,
            "favorites": {"enabled": True, "boost": boost, "exploration": exploration}}


# ---- config 校验 ----

def _load(tmp_path: Path, music_extra: dict) -> dict:
    inp = tmp_path / "in"
    inp.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    _track(lib, "calm", "a.mp3")
    raw = {"input": {"dir": str(inp)}, "output": {"dir": str(tmp_path / "out")},
           "music": {"library": str(lib), "select": "category", "category": "calm",
                     **music_extra}}
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(cfg_path)


def test_config_favorites_default_off(tmp_path: Path) -> None:
    cfg = _load(tmp_path, {})
    assert cfg["music"]["favorites"] == {"enabled": False, "boost": 3.0, "exploration": 0.25}


def test_config_favorites_merge_partial(tmp_path: Path) -> None:
    cfg = _load(tmp_path, {"favorites": {"enabled": True, "boost": 5}})
    assert cfg["music"]["favorites"] == {"enabled": True, "boost": 5, "exploration": 0.25}


@pytest.mark.parametrize("bad", [
    {"favorites": {"enabled": True, "unknown_key": 1}},
    {"favorites": {"boost": 0.5}},          # < 1
    {"favorites": {"boost": 500}},          # > 100
    {"favorites": {"exploration": 1.5}},    # > 1
    {"favorites": {"exploration": -0.1}},   # < 0
    {"favorites": {"enabled": "yes"}},      # 非 bool
    {"favorites": [1, 2]},                  # 非 mapping
])
def test_config_favorites_rejects_bad(tmp_path: Path, bad: dict) -> None:
    with pytest.raises(ConfigError, match="favorites"):
        _load(tmp_path, bad)


# ---- favorites_scores 序列语义 ----

def test_favorites_scores_sequence_semantics(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    t1 = _track(lib, "calm", "t1.mp3")
    t2 = _track(lib, "calm", "t2.mp3")
    t3 = _track(lib, "calm", "t3.mp3")
    t4 = _track(lib, "calm", "t4.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    # vA：自动选 t1 → Steven 点名换成 t2（末条 explicit）→ t2 +2、t1 -1
    # vB：纯自动 t3（末条非 explicit）→ 不计分（接受 ≠ 偏好）
    # vC：直接点名 t4 → +2
    _write_jsonl(selection_log_path(ledger), [
        _sel_row(t1, source_video="vA.mp4", explicit=False),
        _sel_row(t3, source_video="vB.mp4", explicit=False),
        _sel_row(t2, source_video="vA.mp4", explicit=True),
        _sel_row(t4, source_video="vC.mp4", explicit=True),
    ])
    scores, warn = favorites_scores(ledger)
    assert warn is None
    assert scores[str(t2.resolve())] == 2 and scores["t2.mp3"] == 2
    assert scores[str(t1.resolve())] == -1 and scores["t1.mp3"] == -1
    assert scores[str(t4.resolve())] == 2
    assert str(t3.resolve()) not in scores and "t3.mp3" not in scores


def test_favorites_scores_missing_log(tmp_path: Path) -> None:
    assert favorites_scores(tmp_path / "exports" / "usage_ledger.jsonl") == ({}, None)


def test_middle_explicit_stays_neutral(tmp_path: Path) -> None:
    # 同一视频编辑序列：自动 A → 点名 B → 点名 C。末条 C +2、被换掉的自动 A -1、
    # 中间 explicit B 中性（缺明确 rejected 信号不惩罚；Codex verdict A 确认此为预期）
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    b = _track(lib, "calm", "b.mp3")
    c = _track(lib, "calm", "c.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    _write_jsonl(selection_log_path(ledger), [
        _sel_row(a, source_video="vA.mp4", explicit=False),
        _sel_row(b, source_video="vA.mp4", explicit=True),
        _sel_row(c, source_video="vA.mp4", explicit=True),
    ])
    scores, _ = favorites_scores(ledger)
    assert scores.get(str(c.resolve())) == 2
    assert str(b.resolve()) not in scores          # 中间 explicit 中性
    assert scores.get(str(a.resolve())) == -1


def test_scores_isolated_across_configs(tmp_path: Path) -> None:
    # 两个 deal 用各自 config 但同名视频（客厅.mp4）：折叠键含 config → 不串序列（Codex P1）
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    b = _track(lib, "calm", "b.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    rowA = _sel_row(a, source_video="客厅.mp4", explicit=False)  # dealA：自动选 a
    rowA_pick = _sel_row(b, source_video="客厅.mp4", explicit=True)  # dealA：换成 b → b+2/a-1
    rowB = _sel_row(a, source_video="客厅.mp4", explicit=True)  # dealB：直接点名 a → a+2
    rowB["config"] = "dealB.yaml"
    _write_jsonl(selection_log_path(ledger), [rowA, rowA_pick, rowB])
    scores, _ = favorites_scores(ledger)
    # 若误按 source_video 单键折叠：三行并成一序列，a 会被 -1 抵消。含 config 则两序列独立：
    # dealA 得 b+2/a-1，dealB 得 a+2 → a 净分 = -1+2 = 1，b = 2
    assert scores.get(str(a.resolve())) == 1
    assert scores.get(str(b.resolve())) == 2


# ---- 四态 + 行为=现状 ----

def test_off_and_no_data_match_current_behavior(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _track(lib, "calm", "a.mp3")
    _track(lib, "calm", "b.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"  # 无台账文件、无选曲记录
    off_cfg = {"enabled": True, "library": str(lib), "select": "category",
               "category": "calm", "no_repeat_window": 30}
    on_cfg = _fav_cfg(lib)
    for seed in range(6):
        ch_off = choose_music(off_cfg, random.Random(seed), ledger_path=ledger)
        ch_on = choose_music(on_cfg, random.Random(seed), ledger_path=ledger)
        assert ch_off.favorites_mode == "off"
        assert ch_on.favorites_mode == "no_data"
        assert ch_off.path == ch_on.path  # 无偏好信号：同 seed 同曲，行为=现状


def test_no_ledger_path_favorites_off(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _track(lib, "calm", "a.mp3")
    ch = choose_music(_fav_cfg(lib), random.Random(0), ledger_path=None)
    assert ch.dedup_mode == "disabled" and ch.favorites_mode == "off"  # 无台账读不到记录


def test_weighted_prefers_explicit_pick(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    fav = _track(lib, "calm", "fav.mp3")
    other = _track(lib, "calm", "other.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    # 自动选 other 被换成 fav → fav +2 / other -1；全池有信号 → 无探索位，纯加权
    _write_jsonl(selection_log_path(ledger), [
        _sel_row(other, source_video="vA.mp4", explicit=False),
        _sel_row(fav, source_video="vA.mp4", explicit=True),
    ])
    hits = 0
    for seed in range(50):
        ch = choose_music(_fav_cfg(lib, boost=100.0, exploration=1.0),
                          random.Random(seed), ledger_path=ledger)
        assert ch.favorites_mode == "weighted"  # 无无信号曲 → exploration=1.0 也不探索
        hits += ch.path == fav
    assert hits >= 45  # 权重 100 : 0.01 → 压倒性偏向 fav


def test_explore_picks_fresh_track(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    fav = _track(lib, "calm", "fav.mp3")
    _track(lib, "calm", "bad.mp3")
    fresh = _track(lib, "calm", "fresh.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    _write_jsonl(selection_log_path(ledger), [
        _sel_row(lib / "calm" / "bad.mp3", source_video="vA.mp4", explicit=False),
        _sel_row(fav, source_video="vA.mp4", explicit=True),
    ])
    for seed in range(6):  # exploration=1.0 → 必进探索位，且只从无信号曲（fresh）里选
        ch = choose_music(_fav_cfg(lib, exploration=1.0), random.Random(seed),
                          ledger_path=ledger)
        assert ch.favorites_mode == "explore" and ch.path == fresh, (seed, ch)


def test_dedup_window_beats_favorites(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    fav = _track(lib, "calm", "fav.mp3")
    c = _track(lib, "calm", "c.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    _write_jsonl(selection_log_path(ledger),
                 [_sel_row(fav, source_video="vA.mp4", explicit=True)])  # fav 是最爱
    _write_jsonl(ledger, [_usage_row(fav, "2026-07-17T00:00:00+00:00",
                                     source_video="vA.mp4")])  # 但 fav 在去重窗口内
    for seed in range(4):
        ch = choose_music(_fav_cfg(lib, boost=100.0), random.Random(seed), ledger_path=ledger)
        assert ch.path == c, (seed, ch)  # 去重硬约束赢：最爱也被排除
        assert ch.dedup_mode == "normal" and ch.excluded_count == 1
        assert ch.favorites_mode == "no_data"  # 剩余池只剩无信号曲


def test_explicit_and_lru_not_affected(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")  # 唯一曲子
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    _write_jsonl(selection_log_path(ledger),
                 [_sel_row(a, source_video="vA.mp4", explicit=True)])
    _write_jsonl(ledger, [_usage_row(a, "2026-07-17T00:00:00+00:00", source_video="vA.mp4")])
    # 池排空 → LRU 兜底：确定性选曲，favorites 不参与
    ch = choose_music(_fav_cfg(lib), random.Random(0), ledger_path=ledger)
    assert ch.dedup_mode == "lru_fallback" and ch.favorites_mode == "off"
    # explicit 点名：favorites 不参与
    ch = choose_music(_fav_cfg(lib), random.Random(0),
                      override_music=str(a.resolve()), ledger_path=ledger)
    assert ch.explicit and ch.favorites_mode == "off"


def test_name_key_fallback_after_library_move(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    fav = _track(lib, "calm", "fav.mp3")
    _track(lib, "calm", "other.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    # 记录里的 selected_track 指向旧库路径（已迁库），只有 selected_name 还对得上
    old = _sel_row(fav, source_video="vA.mp4", explicit=True)
    old["selected_track"] = "D:/oldlib/calm/fav.mp3"
    _write_jsonl(selection_log_path(ledger), [old])
    hits = 0
    for seed in range(50):
        ch = choose_music(_fav_cfg(lib, boost=100.0), random.Random(seed), ledger_path=ledger)
        if ch.favorites_mode == "weighted":
            hits += ch.path == fav
    assert hits >= 45  # name 兜底键让迁库后偏好仍生效


def test_duplicate_basename_no_score_bleed(tmp_path: Path) -> None:
    # 两个分类各有 same.mp3，select=random 全库摊平 → 池含两首同名。只有被点名的那首
    # （resolved path 命中）加权；另一首禁用 name 兜底键不串分（Codex P1 直接锁 finding 2）
    lib = tmp_path / "lib"
    picked = _track(lib, "luxury", "same.mp3")
    _track(lib, "cozy", "same.mp3")               # 同名不同分类
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    _write_jsonl(selection_log_path(ledger),
                 [_sel_row(picked, source_video="vA.mp4", explicit=True)])  # 只点名 luxury/same
    cfg = {"enabled": True, "library": str(lib), "select": "random",
           "no_repeat_window": 30,
           "favorites": {"enabled": True, "boost": 100.0, "exploration": 0.0}}
    hits = weighted = 0
    warned = False
    for seed in range(50):
        ch = choose_music(cfg, random.Random(seed), ledger_path=ledger)
        warned = warned or (ch.warning is not None and "串曲" in ch.warning)
        if ch.favorites_mode == "weighted":
            weighted += 1
            hits += ch.path == picked
    assert weighted >= 45 and hits >= 45   # 只有 luxury/same 被 boost，cozy/same 不继承
    assert warned                          # 串曲抑制告警


def test_selection_log_warning_merged_with_dedup_warning(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    a = _track(lib, "calm", "a.mp3")
    _track(lib, "calm", "b.mp3")
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    good_sel = json.dumps(_sel_row(a, source_video="vA.mp4", explicit=True), ensure_ascii=False)
    _write_jsonl(ledger, [])  # 先建父目录
    selection_log_path(ledger).write_text(good_sel + "\n{broken\n", encoding="utf-8")
    ledger.write_text("not json\n", encoding="utf-8")
    ch = choose_music(_fav_cfg(lib), random.Random(0), ledger_path=ledger)
    assert ch.warning is not None
    assert "使用台账" in ch.warning and "选曲决策记录" in ch.warning  # 两路警告都不丢


def test_config_to_choose_music_end_to_end(tmp_path: Path) -> None:
    cfg = _load(tmp_path, {"favorites": {"enabled": True}})
    lib = Path(cfg["music"]["library"])
    fav = lib / "calm" / "a.mp3"
    ledger = tmp_path / "exports" / "usage_ledger.jsonl"
    assert append_selection_log(
        ledger, _sel_row(fav, source_video="vA.mp4", explicit=True)) is None
    ch = choose_music(cfg["music"], random.Random(0), ledger_path=ledger)
    assert ch.path == fav  # 单曲池：无论 weighted 还是探索都只有它
    assert ch.favorites_mode in {"weighted", "explore"}
