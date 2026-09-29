"""propcut 精选曲池 + personal/commercial 授权开关。"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from pathlib import Path

import pytest
import yaml

from propcut.config import ConfigError, load_config
from propcut.music import choose_music


REPO_ROOT = Path(__file__).resolve().parents[1]


def _pool_file(tmp_path: Path, tracks: str) -> Path:
    path = tmp_path / "pools.yaml"
    path.write_text(
        "version: 1\npools:\n  soft:\n    tracks:\n" + tracks,
        encoding="utf-8",
    )
    return path


def _music_cfg(library: Path, pools_file: Path, scope: str) -> dict:
    return {
        "enabled": True,
        "library": str(library),
        "pool": "soft",
        "pools_file": str(pools_file),
        "license_scope": scope,
        "select": "random",
        "no_repeat_window": 0,
        "favorites": {"enabled": False},
    }


def test_user_selected_scope_requires_explicit_pool_and_track_approval(tmp_path: Path) -> None:
    library = tmp_path / "music"
    library.mkdir()
    (library / "chosen.mp3").write_bytes(b"audio")
    pools = _pool_file(tmp_path, "      - file: chosen.mp3\n        commercial_ok: false\n")
    cfg = _music_cfg(library, pools, "user_selected")
    with pytest.raises(ConfigError, match="user_selected"):
        choose_music(cfg, random.Random(1))
    doc = yaml.safe_load(pools.read_text(encoding="utf-8"))
    doc["pools"]["soft"]["user_selected_allowed"] = True
    pools.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ConfigError, match="user_approved"):
        choose_music(cfg, random.Random(1))
    doc["pools"]["soft"]["tracks"][0]["taste_status"] = "user_approved"
    pools.write_text(yaml.safe_dump(doc), encoding="utf-8")
    choice = choose_music(cfg, random.Random(1))
    assert choice.path == (library / "chosen.mp3").resolve()
    assert choice.commercial_ok is False and choice.license_scope == "user_selected"
    outside = library / "other.mp3"
    outside.write_bytes(b"outside")
    with pytest.raises(ConfigError, match="user_selected"):
        choose_music(cfg, random.Random(1), override_music=str(outside.resolve()))
    cfg.pop("pool")
    with pytest.raises(ConfigError, match="user_selected"):
        choose_music(cfg, random.Random(1))


def test_personal_scope_can_use_track_marked_noncommercial(tmp_path: Path) -> None:
    library = tmp_path / "music"
    library.mkdir()
    track = library / "personal.mp3"
    track.write_bytes(b"x")
    pools = _pool_file(
        tmp_path,
        "      - file: personal.mp3\n"
        "        commercial_ok: false\n"
        "        taste_status: user_approved\n",
    )

    choice = choose_music(_music_cfg(library, pools, "personal"), random.Random(1))

    assert choice.path == track.resolve()
    assert choice.pool == "soft"
    assert choice.license_scope == "personal"
    assert choice.commercial_ok is False


def test_pool_resolves_library_move_by_id_and_verified_hash(tmp_path: Path) -> None:
    library = tmp_path / "music"
    library.mkdir()
    moved = library / "moved.mp3"
    moved.write_bytes(b"audio")
    sha = hashlib.sha256(b"audio").hexdigest()
    db = tmp_path / "library.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE tracks(id INTEGER,local_file_path TEXT,file_hash_sha256 TEXT,status TEXT)")
        con.execute("INSERT INTO tracks VALUES (1,?,?,'review')", (str(moved.resolve()), sha))
    pools = _pool_file(tmp_path, "      - file: old.mp3\n        commercial_ok: false\n")
    doc = yaml.safe_load(pools.read_text(encoding="utf-8"))
    pool = doc["pools"]["soft"]
    pool.update(library_db=str(db.resolve()), user_selected_allowed=True)
    pool["tracks"][0].update(library_track_id=1, sha256=sha, taste_status="user_approved")
    pools.write_text(yaml.safe_dump(doc), encoding="utf-8")
    cfg = _music_cfg(library, pools, "user_selected")
    assert choose_music(cfg, random.Random(1)).path == moved.resolve()
    moved.write_bytes(b"unexpected replacement")
    with pytest.raises(ConfigError, match="SHA256"):
        choose_music(cfg, random.Random(1))


def test_personal_pool_can_reference_absolute_track_outside_commercial_library(
    tmp_path: Path,
) -> None:
    library = tmp_path / "commercial"
    library.mkdir()
    personal_track = tmp_path / "personal" / "owned.mp3"
    personal_track.parent.mkdir()
    personal_track.write_bytes(b"x")
    pools = _pool_file(
        tmp_path,
        f'      - file: "{personal_track.resolve().as_posix()}"\n'
        "        commercial_ok: false\n"
        "        taste_status: user_approved\n",
    )

    choice = choose_music(_music_cfg(library, pools, "personal"), random.Random(1))

    assert choice.path == personal_track.resolve()
    assert choice.commercial_ok is False


def test_commercial_scope_filters_noncommercial_track(tmp_path: Path) -> None:
    library = tmp_path / "music"
    library.mkdir()
    commercial = library / "commercial.mp3"
    personal = library / "personal.mp3"
    commercial.write_bytes(b"x")
    personal.write_bytes(b"x")
    pools = _pool_file(
        tmp_path,
        "      - file: personal.mp3\n"
        "        commercial_ok: false\n"
        "      - file: commercial.mp3\n"
        "        commercial_ok: true\n",
    )

    choice = choose_music(_music_cfg(library, pools, "commercial"), random.Random(1))

    assert choice.path == commercial.resolve()
    assert choice.commercial_ok is True
    assert choice.license_scope == "commercial"


def test_commercial_scope_fails_closed_if_pool_has_no_commercial_track(tmp_path: Path) -> None:
    library = tmp_path / "music"
    library.mkdir()
    (library / "personal.mp3").write_bytes(b"x")
    pools = _pool_file(
        tmp_path,
        "      - file: personal.mp3\n"
        "        commercial_ok: false\n",
    )

    with pytest.raises(ConfigError, match="commercial_ok=true"):
        choose_music(_music_cfg(library, pools, "commercial"), random.Random(1))


def test_pool_requires_explicit_boolean_commercial_mark(tmp_path: Path) -> None:
    library = tmp_path / "music"
    library.mkdir()
    (library / "unknown.mp3").write_bytes(b"x")
    pools = _pool_file(tmp_path, "      - file: unknown.mp3\n")

    with pytest.raises(ConfigError, match="commercial_ok"):
        choose_music(_music_cfg(library, pools, "personal"), random.Random(1))


def test_commercial_explicit_file_outside_commercial_library_fails_closed(tmp_path: Path) -> None:
    library = tmp_path / "commercial"
    library.mkdir()
    outside = tmp_path / "personal.mp3"
    outside.write_bytes(b"x")
    cfg = {
        "enabled": True,
        "library": str(library.resolve()),
        "license_scope": "commercial",
        "select": "file",
        "file": str(outside.resolve()),
    }

    with pytest.raises(ConfigError, match="无法确认可商用"):
        choose_music(cfg, random.Random(1))


def test_profile_expands_pool_while_scope_stays_runtime_switch(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    library = tmp_path / "music"
    library.mkdir()
    (library / "x.mp3").write_bytes(b"x")
    _pool_file(
        tmp_path,
        "      - file: x.mp3\n"
        "        commercial_ok: true\n",
    )
    (tmp_path / "profiles.yaml").write_text(
        "profiles:\n  walkthrough_soft:\n    pool: soft\n    volume: 0.6\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        "music:\n  enabled: true\n"
        f'  library: "{library.as_posix()}"\n'
        '  profiles_file: "profiles.yaml"\n'
        '  pools_file: "pools.yaml"\n'
        "  profile: walkthrough_soft\n"
        "  license_scope: personal\n",
        encoding="utf-8",
    )

    music = load_config(cfg_path)["music"]

    assert music["pool"] == "soft"
    assert music["license_scope"] == "personal"
    assert music["volume"] == 0.6
    assert Path(music["pools_file"]) == (tmp_path / "pools.yaml").resolve()


def test_profile_pool_conflicts_with_explicit_category_selection(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    library = tmp_path / "music"
    library.mkdir()
    (tmp_path / "profiles.yaml").write_text(
        "profiles:\n  walkthrough_soft:\n    pool: soft\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        "music:\n  enabled: true\n"
        f'  library: "{library.as_posix()}"\n'
        '  profiles_file: "profiles.yaml"\n'
        "  profile: walkthrough_soft\n"
        "  select: category\n"
        "  category: cozy_warm\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="冲突"):
        load_config(cfg_path)


def test_invalid_license_scope_rejected(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    library = tmp_path / "music"
    library.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f'input:\n  dir: "{(tmp_path / "in").as_posix()}"\n'
        f'output:\n  dir: "{(tmp_path / "out").as_posix()}"\n'
        "music:\n  enabled: true\n"
        f'  library: "{library.as_posix()}"\n'
        "  license_scope: anything\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="license_scope"):
        load_config(cfg_path)


