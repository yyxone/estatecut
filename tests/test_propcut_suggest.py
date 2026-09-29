"""propcut 选曲推荐器（P0-1）单测 — 自建 tmp SQLite + 假曲库树，不碰真曲库、不跑 ffmpeg。

覆盖：过滤闸 fail closed（rejected / 未分析 / vocals 高 / red_flags / 文件缺失 / 库外路径）/
BPM 半速倍速等价 / 曲长契合与循环罚 / energy 百分位 / 每艺术家上限 /
strict_fallback vs 合并池 / DB 缺降级 / 只读（DB 字节不变）/ selection 键过 config 校验。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from propcut.config import load_config
from propcut.suggest import (SuggestUnavailable, _bpm_score, _duration_score,
                             fetch_candidates, fetch_pool_candidates, rank_candidates, resolve_library_db, suggest)

_SCHEMA = """
CREATE TABLE tracks (id INTEGER PRIMARY KEY, title TEXT, artist TEXT,
    local_file_path TEXT, duration_seconds REAL, bpm REAL,
    status TEXT, usage_tier TEXT, category TEXT);
CREATE TABLE track_analysis (id INTEGER PRIMARY KEY, track_id INTEGER,
    bpm REAL, rms_energy REAL, vocals_p REAL, red_flags TEXT);
"""


def _make_library(tmp_path: Path) -> tuple[Path, Path]:
    """标准布局曲库根 + commercial 货架目录。"""
    root = tmp_path / "musiclib"
    shelf = root / "music" / "01_approved" / "commercial"
    for cat in ("cozy_warm", "luxury_elegant"):
        (shelf / cat).mkdir(parents=True, exist_ok=True)
    (root / "db").mkdir(parents=True, exist_ok=True)
    return root, shelf


def _add_track(con: sqlite3.Connection, shelf: Path, tid: int, *, title: str,
               artist: str = "A", category: str = "cozy_warm", dur: float = 120.0,
               bpm: float | None = 90.0, rms: float = 0.15, vocals: float | None = 0.1,
               flags: str = "[]", status: str = "approved", tier: str = "commercial",
               write_file: bool = True, analysis: bool = True,
               path_override: str | None = None) -> None:
    p = path_override or str(shelf / category / f"{title}.mp3")
    if write_file and path_override is None:
        Path(p).write_bytes(b"x")
    con.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, title, artist, p, dur, bpm, status, tier, category))
    if analysis:
        con.execute("INSERT INTO track_analysis VALUES (?,?,?,?,?,?)",
                    (tid, tid, bpm, rms, vocals, flags))


def _make_db(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(root / "db" / "music_library.sqlite")
    con.executescript(_SCHEMA)
    return con


def test_resolve_library_db_layout(tmp_path: Path) -> None:
    root, shelf = _make_library(tmp_path)
    assert resolve_library_db(shelf) == root / "db" / "music_library.sqlite"
    assert resolve_library_db(tmp_path / "randomlib") is None


def test_filter_gate_fail_closed(tmp_path: Path) -> None:
    root, shelf = _make_library(tmp_path)
    con = _make_db(root)
    _add_track(con, shelf, 1, title="good")
    _add_track(con, shelf, 2, title="rejected", status="rejected")
    _add_track(con, shelf, 3, title="review", status="review")
    _add_track(con, shelf, 4, title="personal_tier", tier="personal")
    _add_track(con, shelf, 5, title="no_analysis", analysis=False)
    _add_track(con, shelf, 6, title="vocal_heavy", vocals=0.8)
    _add_track(con, shelf, 7, title="vocals_null", vocals=None)
    _add_track(con, shelf, 8, title="flagged", flags='["political"]')
    _add_track(con, shelf, 9, title="file_missing", write_file=False)
    _add_track(con, shelf, 10, title="outside_lib",
               path_override=str(tmp_path / "elsewhere.mp3"))
    (tmp_path / "elsewhere.mp3").write_bytes(b"x")
    con.commit(); con.close()

    cands = fetch_candidates(root / "db" / "music_library.sqlite",
                             ["cozy_warm", "luxury_elegant"], shelf)
    assert [c["title"] for c in cands] == ["good"]


def test_bpm_half_double_equivalence() -> None:
    assert _bpm_score(123.0, [110, 130]) == 1.0
    assert _bpm_score(61.5, [110, 130]) == 1.0    # 半速等价
    assert _bpm_score(246.0, [110, 130]) == 1.0   # 记录成倍速也等价
    assert _bpm_score(90.0, [110, 130]) < 1.0
    assert _bpm_score(None, [110, 130]) == 0.0    # 有目标区间但无数据 → fail closed
    assert _bpm_score(None, None) == 0.5          # 无目标区间 → 中性


def test_selected_pool_keeps_user_choices_despite_machine_flags(tmp_path: Path) -> None:
    root, shelf = _make_library(tmp_path)
    con = _make_db(root)
    _add_track(con, shelf, 1, title="vocal", vocals=0.9, flags='["political"]', status="review")
    _add_track(con, shelf, 2, title="unanalysed", analysis=False, status="review")
    _add_track(con, shelf, 3, title="outside_selected")
    con.commit()
    con.close()
    paths = [shelf / "cozy_warm" / f"{s}.mp3" for s in ("vocal", "unanalysed")]
    db = root / "db" / "music_library.sqlite"
    assert fetch_pool_candidates(db, paths, commercial=False) == []
    result = fetch_pool_candidates(db, paths, commercial=False, user_selected=True)
    assert {r["title"] for r in result} == {"vocal", "unanalysed"}
    assert result[0]["audio_hints"]["red_flags"] == ["political"]


def test_duration_score_loop_penalty() -> None:
    assert _duration_score(120.0, 60.0) == 1.0
    assert _duration_score(60.0, 60.0) == 1.0
    assert _duration_score(30.0, 60.0) == pytest.approx(0.4)  # 一半长 → 0.5*0.8
    assert _duration_score(0.0, 60.0) == 0.0


def test_rank_prefers_fit_and_caps_artist(tmp_path: Path) -> None:
    root, shelf = _make_library(tmp_path)
    con = _make_db(root)
    # 同 artist 三首全高分 → 只能进 2 首；第三名让给别人
    for i, title in enumerate(("a1", "a2", "a3")):
        _add_track(con, shelf, i + 1, title=title, artist="Prolific", bpm=90.0, dur=120.0)
    _add_track(con, shelf, 4, title="other", artist="Someone", bpm=90.0, dur=120.0)
    _add_track(con, shelf, 5, title="short_offbpm", artist="Third", bpm=140.0, dur=20.0)
    con.commit(); con.close()

    cands = fetch_candidates(root / "db" / "music_library.sqlite", ["cozy_warm"], shelf)
    ranked = rank_candidates(cands, target_dur=60.0,
                             selection={"bpm_range": [80, 100], "energy": "medium"}, top=4)
    artists = [r["artist"] for r in ranked]
    assert artists.count("Prolific") == 2
    assert "Someone" in artists
    assert ranked[-1]["title"] == "short_offbpm"  # 短曲 + 偏 BPM 必垫底
    assert ranked[0]["score"] > ranked[-1]["score"]


def _write_cfg(tmp_path: Path, shelf: Path, music_extra: str = "") -> Path:
    (tmp_path / "in").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    cfg = tmp_path / "propcut.yaml"
    cfg.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        f'music:\n  enabled: true\n  library: "{shelf.as_posix()}"\n{music_extra}',
        encoding="utf-8")
    return cfg


def test_suggest_end_to_end_readonly(tmp_path: Path) -> None:
    root, shelf = _make_library(tmp_path)
    con = _make_db(root)
    _add_track(con, shelf, 1, title="cozy_song", category="cozy_warm", bpm=75.0)
    _add_track(con, shelf, 2, title="lux_song", category="luxury_elegant", bpm=70.0)
    con.commit(); con.close()
    db = root / "db" / "music_library.sqlite"
    digest_before = hashlib.sha256(db.read_bytes()).hexdigest()

    pf = tmp_path / "profiles.yaml"
    pf.write_text(
        "profiles:\n  demo:\n    categories: [luxury_elegant, cozy_warm]\n"
        "    selection: {bpm_range: [60, 80], energy: low}\n"
        '    notes: "n"\n', encoding="utf-8")
    cfg = _write_cfg(tmp_path, shelf, '  profiles_file: "profiles.yaml"\n  profile: demo\n')

    # 默认合并池：两个分类都进（strict 语义下 cozy_warm 会被 luxury 挡掉）
    result = suggest(cfg, duration=60.0, top=5)
    assert result["pool_size"] == 2
    assert {s["category"] for s in result["suggestions"]} == {"cozy_warm", "luxury_elegant"}
    assert result["selection"] == {"bpm_range": [60, 80], "energy": "low"}

    strict = suggest(cfg, duration=60.0, top=5, strict_fallback=True)
    assert strict["categories"] == ["luxury_elegant"]
    assert [s["title"] for s in strict["suggestions"]] == ["lux_song"]

    # 零写入：DB 字节不变，无 -wal/-journal 残留
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest_before
    assert not (db.parent / (db.name + "-wal")).exists()
    assert not (db.parent / (db.name + "-journal")).exists()


def test_suggest_db_missing_degrades(tmp_path: Path) -> None:
    root, shelf = _make_library(tmp_path)  # 不建 DB 文件
    cfg = _write_cfg(tmp_path, shelf)
    with pytest.raises(SuggestUnavailable):
        suggest(cfg, duration=60.0)

    from propcut.cli import main
    assert main(["suggest", "--config", str(cfg), "--duration", "60"]) == 2


def test_selection_key_passes_config_validation(tmp_path: Path) -> None:
    """profile 带 selection 节 → load_config 不炸，且 selection 不进运行配置。"""
    root, shelf = _make_library(tmp_path)
    pf = tmp_path / "profiles.yaml"
    pf.write_text(
        "profiles:\n  demo:\n    categories: [cozy_warm]\n"
        "    selection: {bpm_range: [60, 80], energy: low}\n", encoding="utf-8")
    cfg_path = _write_cfg(tmp_path, shelf, '  profiles_file: "profiles.yaml"\n  profile: demo\n')
    cfg = load_config(cfg_path)
    assert "selection" not in cfg["music"]
    assert cfg["music"]["categories"] == ["cozy_warm"]


def test_suggest_honors_curated_pool_and_license_scope(tmp_path: Path) -> None:
    root, shelf = _make_library(tmp_path)
    con = _make_db(root)
    _add_track(con, shelf, 1, title="commercial", tier="commercial")
    _add_track(con, shelf, 2, title="personal", tier="personal")
    _add_track(con, shelf, 3, title="not_in_pool", tier="commercial")
    con.commit(); con.close()

    (tmp_path / "profiles.yaml").write_text(
        "profiles:\n  demo:\n    pool: soft\n",
        encoding="utf-8",
    )
    (tmp_path / "pools.yaml").write_text(
        "version: 1\npools:\n  soft:\n    tracks:\n"
        "      - file: cozy_warm/commercial.mp3\n"
        "        commercial_ok: true\n"
        "      - file: cozy_warm/personal.mp3\n"
        "        commercial_ok: false\n",
        encoding="utf-8",
    )
    base = (
        '  profiles_file: "profiles.yaml"\n'
        '  pools_file: "pools.yaml"\n'
        "  profile: demo\n"
    )
    personal_cfg = _write_cfg(tmp_path, shelf, base + "  license_scope: personal\n")
    personal = suggest(personal_cfg, duration=60.0, top=5)
    assert personal["pool"] == "soft"
    assert personal["license_scope"] == "personal"
    assert {s["title"] for s in personal["suggestions"]} == {"commercial", "personal"}

    commercial_cfg = tmp_path / "commercial.yaml"
    commercial_cfg.write_text(
        personal_cfg.read_text(encoding="utf-8").replace(
            "license_scope: personal", "license_scope: commercial"),
        encoding="utf-8",
    )
    commercial = suggest(commercial_cfg, duration=60.0, top=5)
    assert [s["title"] for s in commercial["suggestions"]] == ["commercial"]
