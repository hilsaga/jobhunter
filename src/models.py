"""Persisted and in-memory records for the job-hunter pipeline."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ParsedDocument(BaseModel):
    path: str
    text: str


class ExperienceItem(BaseModel):
    title: str = ""
    organization: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)


class CandidateBaseline(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    email: str = ""
    phone: str = ""
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
    raw_text: str = Field(default="", exclude=True)


class TargetProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    targeted_income: str
    targeted_location: str
    interested_keywords: list[str] = Field(default_factory=list)
    skip_list: list[str] = Field(default_factory=list)
    extra_job_titles: list[str] = Field(default_factory=list)
    candidate: CandidateBaseline = Field(default_factory=CandidateBaseline)
    specializations: list[str] = Field(default_factory=list)
    updated_at: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_extra_job_title(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        titles = value.get("extra_job_titles")
        legacy = value.get("extra_job_title")
        if titles in (None, "", []) and isinstance(legacy, str) and legacy.strip():
            migrated = dict(value)
            migrated["extra_job_titles"] = [legacy.strip()]
            return migrated
        return value

    @field_validator("interested_keywords", mode="before")
    @classmethod
    def _clean_keywords(cls, value: object) -> list[str]:
        if isinstance(value, str):
            from src.textutil import parse_keywords

            return parse_keywords(value)
        if isinstance(value, list):
            cleaned: list[str] = []
            seen: set[str] = set()
            for item in value:
                text = str(item).strip()
                key = text.lower()
                if text and key not in seen:
                    seen.add(key)
                    cleaned.append(text)
            return cleaned
        return []

    @field_validator("skip_list", "extra_job_titles", mode="before")
    @classmethod
    def _clean_skip_list(cls, value: object) -> list[str]:
        if isinstance(value, str):
            from src.textutil import parse_keywords

            return parse_keywords(value)
        if isinstance(value, list):
            cleaned: list[str] = []
            seen: set[str] = set()
            for item in value:
                text = str(item).strip()
                key = text.lower()
                if text and key not in seen:
                    seen.add(key)
                    cleaned.append(text)
            return cleaned
        return []


class Specialization(BaseModel):
    model_config = ConfigDict(extra="ignore")

    area: str
    title: str
    summary: str = ""
    highlights: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    cover_letter: str = ""


class Site(BaseModel):
    site_name: str
    url: str
    location_filter_param: str = ""


class JobListing(BaseModel):
    job_id: str
    title: str
    company: str = ""
    location: str = ""
    url: str
    description: str = ""
    source_site: str = ""


class JobDetails(BaseModel):
    job_id: str
    job_title: str
    company: str = ""
    location: str = ""
    job_description: str = ""
    source_url: str
    applied_timestamp: str
    cv_path_used: str
    cover_letter_path_used: str
    match_score: float = 0.0
    specialization: str = ""
