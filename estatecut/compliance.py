"""Automated real-estate and Xiaohongshu compliance screen."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_yaml
from .resources_path import resource_path
from .schemas import EditPlan, ListingFacts
from .utils import read_json, write_json


def _terms(config: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for category in config.get("high_risk_terms", {}).values():
        values.extend(str(x) for x in category)
    return values


def load_terms(config_path: Path) -> list[str]:
    """跨 CLI 共享的违禁词加载器：从 yaml 取结构化词表（中英文皆扫，大小写无关由调用方负责）。

    canonical 入口——talkcut 侧应复用此函数，不要再用正则刮 yaml 原文
    （那样会漏英文 Fair-Housing 词、把含标点词碎片化误报）。
    """
    return _terms(load_yaml(config_path))


def _scan_text(label: str, text: str, terms: list[str]) -> list[dict[str, str]]:
    lower = text.lower()
    findings = []
    for term in terms:
        if term and term.lower() in lower:
            findings.append(
                {
                    "field": label,
                    "term": term,
                    "severity": "high",
                    "suggestion": "Use factual, sourced wording and remove absolute, guarantee, protected-class, investment, or off-platform claims.",
                }
            )
    return findings


def scan_compliance(
    facts: ListingFacts,
    style_path: Path,
    out_markdown: Path,
    edit_plan: EditPlan | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    config = load_yaml(config_path or resource_path("xhs_compliance.yaml"))
    terms = _terms(config)
    findings = []
    findings.extend(_scan_text("title_safe", facts.title_safe, terms))
    findings.extend(_scan_text("caption_draft", facts.caption_draft, terms))
    if edit_plan:
        for clip in edit_plan.clips:
            if clip.overlay_text:
                findings.extend(_scan_text(f"overlay_text:{clip.segment_id}", clip.overlay_text, terms))
    warnings: list[str] = [
        "This is an automated compliance screen, not legal advice.",
        "Human review is mandatory before publishing.",
    ]
    if facts.facts_must_be_verified_by_human:
        warnings.append("listing_facts.json says facts must be verified by a human before publishing.")
    if facts.unit_visibility == "model_unit" or facts.is_model_unit:
        warnings.append("Model-unit disclosure may be required.")
    if facts.unit_visibility == "amenity_only":
        warnings.append("Amenity-only footage must not be presented as an actual unit tour.")
    music = facts.music or {}
    if music.get("use_music") and not music.get("license_confirmed"):
        warnings.append("Music requested but license is not confirmed; renderer must not use music.")
    report = {"findings": findings, "warnings": warnings, "style_path": str(style_path)}
    json_path = out_markdown.with_suffix(".json")
    write_json(json_path, report)
    lines = ["# Compliance Report", ""]
    for warning in warnings:
        lines.append(f"- WARNING: {warning}")
    lines.append("")
    lines.append("## Findings")
    if findings:
        for item in findings:
            lines.append(f"- {item['severity'].upper()} `{item['term']}` in `{item['field']}`: {item['suggestion']}")
    else:
        lines.append("- No configured high-risk terms found in scanned text.")
    out_markdown.parent.mkdir(parents=True, exist_ok=True)
    out_markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def scan_compliance_from_files(facts_path: Path, style_path: Path, out_markdown: Path, plan_path: Path | None = None) -> dict[str, Any]:
    facts = ListingFacts.model_validate(read_json(facts_path))
    plan = EditPlan.model_validate(read_json(plan_path)) if plan_path else None
    return scan_compliance(facts, style_path, out_markdown, plan)
