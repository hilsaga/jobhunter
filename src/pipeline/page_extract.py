"""Read job boards and application forms from page HTML or MCP snapshots."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

from src.models import JobListing, TargetProfile
from src.textutil import collapse_ws, html_to_text, slugify

_ANCHOR = re.compile(r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_HEADING = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")
_JOB_HINTS = ("/job", "/jobs/", "jobid", "job_id", "jk=", "/vacancy", "/opening", "position=")
_SKIP_INPUTS = {"hidden", "submit", "button", "image", "reset"}
_AMBIGUOUS = (
    "salary",
    "compensation",
    "expected pay",
    "remuneration",
    "pay rate",
    "notice period",
    "visa",
    "sponsor",
    "authorised",
    "authorized to work",
    "gender",
    "ethnicity",
    "race",
    "disability",
    "veteran",
    "pronoun",
    "how did you hear",
    "why do you",
    "cover letter",
    "additional information",
    "linkedin",
    "website",
    "portfolio",
    "github",
    "years of",
    "are you",
    "do you ",
    "have you",
)


@dataclass
class FormField:
    name: str = ""
    field_id: str = ""
    label: str = ""
    input_type: str = "text"
    required: bool = False
    options: list[str] = field(default_factory=list)


@dataclass
class FieldFill:
    field: FormField
    value: str


@dataclass
class FormPlan:
    auto_fields: list[FieldFill]
    human_fields: list[FormField]
    file_inputs: list[FormField]
    blocked_reason: str | None


def extract_listings(html: str, base_url: str, site_name: str) -> list[JobListing]:
    listings: list[JobListing] = []
    seen: set[str] = set()
    for href, inner in _ANCHOR.findall(html):
        if not any(hint in href.lower() for hint in _JOB_HINTS):
            continue
        url = urljoin(base_url, href.strip())
        job_id = job_id_from_url(url)
        if job_id in seen:
            continue
        seen.add(job_id)
        title = collapse_ws(re.sub(r"(?s)<[^>]+>", " ", inner)) or "Untitled role"
        listings.append(
            JobListing(
                job_id=job_id,
                title=title,
                url=url,
                source_site=site_name,
            )
        )
    return listings


def job_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("jobId", "job_id", "jk", "id", "vacancyId"):
        values = query.get(key)
        if values and values[0].strip():
            return slugify(values[0], fallback="job", limit=80)
    segment = parsed.path.rstrip("/").split("/")[-1] if parsed.path else ""
    if segment:
        return slugify(segment, fallback="job", limit=80)
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def extract_heading(html: str) -> str:
    match = _HEADING.search(html)
    if match is None:
        return ""
    return collapse_ws(re.sub(r"(?s)<[^>]+>", " ", match.group(1)))


def application_mode(text: str) -> str:
    lowered = text.lower()
    if "easy apply" in lowered or "quick apply" in lowered:
        return "easy"
    if "apply on company" in lowered or "external site" in lowered or "apply on employer" in lowered:
        return "external"
    if "apply" in lowered:
        return "easy"
    return "unknown"


def plan_form(html: str, profile: TargetProfile) -> FormPlan:
    fields = extract_fields(html)
    blocked = page_block_reason(html_to_text(html), fields)
    auto_fields: list[FieldFill] = []
    human_fields: list[FormField] = []
    file_inputs: list[FormField] = []
    for form_field in fields:
        if form_field.input_type in _SKIP_INPUTS:
            continue
        if form_field.input_type == "file":
            file_inputs.append(form_field)
            continue
        blob = _blob(form_field)
        if form_field.input_type == "password" or _is_ambiguous(blob):
            human_fields.append(form_field)
            continue
        value = _auto_value(blob, profile)
        if value:
            auto_fields.append(FieldFill(field=form_field, value=value))
            continue
        if form_field.required or form_field.input_type in {"select", "radio", "checkbox"}:
            human_fields.append(form_field)
    return FormPlan(
        auto_fields=auto_fields,
        human_fields=human_fields,
        file_inputs=file_inputs,
        blocked_reason=blocked,
    )


def page_block_reason(text: str, fields: list[FormField]) -> str | None:
    lowered = text.lower()
    if any(token in lowered for token in ("captcha", "recaptcha", "hcaptcha", "verify you are human", "i'm not a robot")):
        return "captcha"
    if any(token in lowered for token in ("two-factor", "2fa", "verification code", "one-time code")):
        return "2fa"
    password_fields = [item for item in fields if item.input_type == "password"]
    file_fields = [item for item in fields if item.input_type == "file"]
    textish = [item for item in fields if item.input_type in {"text", "email", "password"}]
    if password_fields and not file_fields and len(textish) <= 3:
        return "login"
    return None


def css_selector(form_field: FormField) -> str:
    if form_field.field_id:
        return f"#{form_field.field_id}"
    if form_field.name:
        safe = form_field.name.replace("'", "")
        return f"[name='{safe}']"
    return ""


def extract_fields(html: str) -> list[FormField]:
    parser = _FormHTMLParser()
    parser.feed(html)
    parser.close()
    for form_field in parser.fields:
        if form_field.field_id and form_field.field_id in parser.labels:
            form_field.label = parser.labels[form_field.field_id]
        if not form_field.label:
            form_field.label = form_field.name or form_field.field_id
    return parser.fields


class _FormHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: list[FormField] = []
        self.labels: dict[str, str] = {}
        self._in_label = False
        self._label_for: str | None = None
        self._label_parts: list[str] = []
        self._in_option = False
        self._option_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "label":
            self._in_label = True
            self._label_for = attr.get("for") or None
            self._label_parts = []
        if tag == "option" and self.fields and self.fields[-1].input_type == "select":
            self._in_option = True
            self._option_parts = []
        if tag in {"input", "textarea", "select"}:
            self.fields.append(_field_from_tag(tag, attr))

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._in_option and self.fields:
            label = collapse_ws(" ".join(self._option_parts))
            if label:
                self.fields[-1].options.append(label)
            self._in_option = False
        if tag == "label" and self._in_label:
            text = collapse_ws(" ".join(self._label_parts))
            if self._label_for and text:
                self.labels[self._label_for] = text
            elif text and self.fields and not self.fields[-1].label:
                self.fields[-1].label = text
            self._in_label = False

    def handle_data(self, data: str) -> None:
        if self._in_option:
            self._option_parts.append(data)
        if self._in_label:
            self._label_parts.append(data)


def _field_from_tag(tag: str, attr: dict[str, str]) -> FormField:
    if tag == "textarea":
        input_type = "textarea"
    elif tag == "select":
        input_type = "select"
    else:
        input_type = (attr.get("type") or "text").lower()
    required = "required" in attr or attr.get("aria-required", "").lower() == "true"
    label = attr.get("aria-label") or attr.get("placeholder") or ""
    return FormField(
        name=attr.get("name", ""),
        field_id=attr.get("id", ""),
        label=label,
        input_type=input_type,
        required=required,
    )


def _blob(form_field: FormField) -> str:
    return " ".join([form_field.name, form_field.field_id, form_field.label]).lower()


def _is_ambiguous(blob: str) -> bool:
    return any(token in blob for token in _AMBIGUOUS)


def _auto_value(blob: str, profile: TargetProfile) -> str:
    candidate = profile.candidate
    parts = candidate.name.split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    if any(token in blob for token in ("e-mail", "email")):
        return candidate.email
    if any(token in blob for token in ("phone", "mobile", "tel")):
        return candidate.phone
    if "first name" in blob or "given name" in blob:
        return first
    if "last name" in blob or "family name" in blob or "surname" in blob:
        return last
    if any(token in blob for token in ("full name", "your name", "candidate name")) or blob.strip() in {
        "name",
        "name name",
    }:
        return candidate.name
    if "name" in blob and "user" not in blob and "company" not in blob and "file" not in blob:
        return candidate.name
    if any(token in blob for token in ("location", "city")):
        return profile.targeted_location
    return ""
