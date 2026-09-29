"""talkcut qaexport.compliance_screen 单测 —— 中英文违禁词都要扫到（回归 P1-1）。

修复前：正则刮 yaml 原文 + `not t.isascii()` 漏掉全部英文 Fair-Housing 词、
把 `100%有效` 这类含标点词碎片化（整词漏报 + 碎片误报）。
修复后：复用 estatecut.compliance.load_terms 结构化词表 + 大小写无关子串匹配。
"""
from __future__ import annotations

from pathlib import Path

from talkcut import qaexport


def _screen(tmp_path: Path, srt_body: str) -> list[str]:
    srt = "1\n00:00:01,000 --> 00:00:03,000\n" + srt_body + "\n"
    (tmp_path / "字幕.srt").write_text(srt, encoding="utf-8")
    hits, check = qaexport.compliance_screen(tmp_path)
    # R4 round-2：真扫过的路径必须报 PASS 执行态（advisory，不因命中降级）
    assert check["status"] == "PASS"
    return hits


def test_chinese_term_hit(tmp_path):
    hits = _screen(tmp_path, "这是最好的学区")
    assert any("最好" in h for h in hits)


def test_english_fair_housing_hit(tmp_path):
    # 核心回归：英文 FH 词以前被 not isascii() 整段漏掉
    hits = _screen(tmp_path, "great place, no kids allowed")
    assert any(h.lower() == "no kids" for h in hits)


def test_english_case_insensitive(tmp_path):
    hits = _screen(tmp_path, "GUARANTEED RENT for two years")
    assert any(h.lower() == "guaranteed rent" for h in hits)


def test_clean_text_no_false_positive(tmp_path):
    hits = _screen(tmp_path, "两室一厅，步行到地铁五分钟")
    assert hits == []


def test_missing_srt_returns_empty(tmp_path):
    hits, check = qaexport.compliance_screen(tmp_path)
    assert hits == []
    # 无 SRT = 契约性 N/A（不降级）；与"配置缺失没扫成"可区分
    assert check["status"] == "SKIPPED" and check["applicable"] is False


def test_missing_config_reports_unexecuted(tmp_path, monkeypatch):
    (tmp_path / "字幕.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    monkeypatch.setattr(qaexport, "XHS_COMPLIANCE", tmp_path / "不存在.yaml")
    hits, check = qaexport.compliance_screen(tmp_path)
    assert hits == []
    # 配置缺失 = screen 未执行 → 普通 SKIPPED（降 PARTIAL），不能与"已检零命中"混同
    assert check["status"] == "SKIPPED" and check.get("applicable") is not False
