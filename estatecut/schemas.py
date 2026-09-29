"""Pydantic data models for the estatecut pipeline."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MediaFile(BaseModel):
    id: str
    path: str
    filename: str
    sort_index: int
    duration_sec: float
    width: int
    height: int
    fps: float
    codec_name: str | None = None
    audio_present: bool
    rotation: int | None = None
    created_time: str | None = None
    source_order_reason: str


class Segment(BaseModel):
    id: str
    media_id: str
    source_path: str
    start_sec: float
    end_sec: float
    duration_sec: float
    thumbnails: list[str] = Field(default_factory=list)
    proxy_path: str | None = None
    segment_index: int
    original_order: int
    shot_id: str | None = None


class LocalQuality(BaseModel):
    segment_id: str
    brightness_score: float
    contrast_score: float
    sharpness_score: float
    exposure_risk: Literal["none", "too_dark", "too_bright", "mixed", "unknown"]
    motion_score: float
    floor_wall_risk: float
    privacy_risk: float
    local_usable_score: float
    local_reject_reasons: list[str] = Field(default_factory=list)


class GeminiAnalysis(BaseModel):
    segment_id: str
    mode: str
    room_type: str
    confidence: float
    visual_description: str
    selling_points: list[str] = Field(default_factory=list)
    defects: list[str] = Field(default_factory=list)
    composition_score: float
    lighting_score: float
    movement_score: float
    publishability_score: float
    hero_shot_score: float
    cover_candidate_score: float
    needs_human_review: bool
    unsafe_or_private_elements: list[str] = Field(default_factory=list)
    notes: str


class RankedSegment(BaseModel):
    segment_id: str
    final_score: float
    keep_recommendation: Literal["keep", "maybe", "reject"]
    reason: str
    room_type: str
    selected_for_edit: bool = False


class EditClip(BaseModel):
    segment_id: str
    source_path: str
    start_sec: float
    end_sec: float
    duration_sec: float
    room_type: str
    reason: str
    fit_mode: str
    audio: str
    overlay_text: str | None = None
    subtitle_ass_path: str | None = None


class EditPlan(BaseModel):
    project_id: str
    input_folder: str
    output_video: str
    target_platforms: list[str]
    target_duration_sec: float
    aspect_ratio: str
    resolution: str
    clips: list[EditClip]
    cover_candidates: list[str] = Field(default_factory=list)
    compliance_required: bool
    human_review_required: bool
    generated_at: str


class ListingFacts(BaseModel):
    property_id: str
    public_location: str | None = None
    property_type: str | None = None
    unit_visibility: str = "actual_unit"
    is_model_unit: bool = False
    title_safe: str = ""
    caption_draft: str = ""
    verified_facts: dict = Field(default_factory=dict)
    facts_must_be_verified_by_human: bool = True
    music: dict = Field(default_factory=dict)
    broker_disclosure: dict = Field(default_factory=dict)
    human_review_required: bool = True
