"""§3 paper-edit — AI 提议起步 EDL + 报告骨架，人拍板。

硬规则：AI **不自动剪任何有内容/未确认的段**。默认 keep = 全片，cuts = []；
gap_audit 里的 gemini_vision（真静音待画面）/ retranscribe（疑似漏转）一律**列候选给人/agent 审**，
不自动进 cuts。真正的 KEEP/CUT/DEDUP 精判由 agent 读 prompts/paperedit_prompt.md 后编辑 edl.json + 人审上 cut_lock。
门控：需 transcript_lock（人审转录稿）才提议。
"""
from __future__ import annotations

import json
from pathlib import Path

from .state import mark_stage, require_lock


def propose_edl(out_dir: Path, project: str) -> dict:
    out_dir = Path(out_dir)
    # 人审转录稿后才进 paper-edit；require_lock 返回校验过的转录稿，不二次读盘
    transcript = require_lock(out_dir, project, "transcript_lock")
    source = transcript["source"]["path"]
    total = float(transcript["source"]["duration_sec"])

    # 默认：保留全片，不自动剪（AI 提议、人拍板）
    edl = {
        "project": project, "source_timeline": source, "human_locked": False,
        "keep_ranges": [{"src_start": 0.0, "src_end": round(total, 3), "label": "全片（待人/agent 精判）"}],
        "cuts": [], "splices": [], "broll_cover": [],
    }
    (out_dir / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(out_dir, project, transcript)
    n_vision = sum(1 for g in transcript.get("gap_audit", []) if g["decision"] == "gemini_vision")
    n_retr = sum(1 for g in transcript.get("gap_audit", []) if g["decision"] == "retranscribe")
    mark_stage(out_dir, project, "paper_edit", "done", [str(out_dir / "edl.json"), str(out_dir / "粗剪报告.md")],
               verified=(out_dir / "edl.json").exists(),
               note=f"默认保留全片（不自动剪）；待审候选 {n_vision} 真静音 + {n_retr} 疑似漏转")
    return edl


def _write_report(out_dir: Path, project: str, transcript: dict) -> None:
    ga = transcript.get("gap_audit", [])
    lines = [f"# 粗剪报告 · {project}", "",
             "> AI 起步：**默认保留全片、不自动剪**（AI 提议、人拍板）。agent 读 prompts/paperedit_prompt.md 后逐段精判，",
             "> 编辑 edl.json（cuts/keep_ranges/splices/broll_cover）+ 人审上 cut_lock 才进 rough-cut。", "",
             "## 待审候选（不自动剪）"]
    vision = [g for g in ga if g["decision"] == "gemini_vision"]
    retr = [g for g in ga if g["decision"] == "retranscribe"]
    lines.append(f"### 真静音段（{len(vision)}）—— 交 Gemini 看画面定 留(B-roll)/剪")
    for g in vision:
        lines.append(f"- [{g['start']}–{g['end']}] {g['result']}")
    lines.append(f"### 疑似漏转段（{len(retr)}）—— 重转/确认内容，**默认保留**")
    for g in retr:
        lines.append(f"- [{g['start']}–{g['end']}] {g['result']}")
    lines += ["", "## 人工精判清单（agent 补）",
              "- 离题/穿帮(bts)、NG忘词(ng_flub)、说一半假开头(false_start)、重复/重说(retake → splice)。",
              "- 每个 CUT 记 category + reason + transcript_quote + 前后台词上下文；重说优先 splice。",
              "- 每条 broll_cover 必填 reason（为什么盖 + 和当下口播什么关系）；建议补 related_transcript_quote 作相关性证据（硬约束 7：空镜要和当下口播相关）。"]
    (out_dir / "粗剪报告.md").write_text("\n".join(lines), encoding="utf-8")
