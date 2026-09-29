from __future__ import annotations

from estatecut.compliance import scan_compliance
from estatecut.schemas import ListingFacts


def test_compliance_flags_high_risk_words(tmp_path) -> None:
    facts = ListingFacts(property_id="p1", title_safe="最好学区 完美公寓", caption_draft="guaranteed rent", verified_facts={})
    report = scan_compliance(facts, __import__("pathlib").Path("configs/style_real_estate.yaml"), tmp_path / "compliance_report.md")
    terms = {item["term"] for item in report["findings"]}
    assert "最好学区" in terms
    assert "完美" in terms
    assert "guaranteed rent" in terms


def test_compliance_allows_safe_factual_caption(tmp_path) -> None:
    facts = ListingFacts(property_id="p1", title_safe="Allston 1B1B Near BU", caption_draft="In-unit laundry. Parking available for rent. Verify current terms.", verified_facts={})
    report = scan_compliance(facts, __import__("pathlib").Path("configs/style_real_estate.yaml"), tmp_path / "compliance_report.md")
    assert report["findings"] == []


def test_unlicensed_music_is_warned_and_not_used(tmp_path) -> None:
    facts = ListingFacts(property_id="p1", title_safe="Allston", caption_draft="Safe factual caption.", verified_facts={}, music={"use_music": True, "license_confirmed": False})
    report = scan_compliance(facts, __import__("pathlib").Path("configs/style_real_estate.yaml"), tmp_path / "compliance_report.md")
    assert any("Music requested" in warning for warning in report["warnings"])
