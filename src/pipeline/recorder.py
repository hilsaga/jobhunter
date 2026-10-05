"""Write the applications/{job_id}/ record after a submission."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from src.models import JobDetails
from src.paths import ProjectPaths
from src.textutil import slugify

logger = logging.getLogger("jobhunter.recorder")


def already_sent(paths: ProjectPaths, job_id: str, source_url: str = "") -> bool:
    """True when this job id or source URL was already saved."""
    slug = slugify(job_id, fallback="job", limit=80)
    if (paths.application_dir(slug) / "job_details.json").is_file():
        return True
    for entry in _load_index(paths):
        if entry.get("job_id") == slug:
            return True
        if source_url and entry.get("source_url") == source_url:
            return True
    if not paths.applications.is_dir():
        return False
    for details_path in paths.applications.glob("*/job_details.json"):
        try:
            payload = json.loads(details_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read %s", details_path)
            continue
        if payload.get("job_id") == slug:
            return True
        if source_url and payload.get("source_url") == source_url:
            return True
    return False


def record_application(
    paths: ProjectPaths,
    details: JobDetails,
    cv_path: Path,
    cover_letter_path: Path,
    *,
    snapshot_path: Path | None = None,
    page_source: str | None = None,
) -> Path:
    """Copy the submitted files and write job metadata. Existing files are checked first."""
    if not cv_path.is_file():
        raise FileNotFoundError(f"CV does not exist: {cv_path}")
    if not cover_letter_path.is_file():
        raise FileNotFoundError(f"Cover letter does not exist: {cover_letter_path}")

    details.job_id = slugify(details.job_id, fallback="job", limit=80)
    job_dir = paths.application_dir(details.job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cv_path, job_dir / "cv_used.pdf")
    shutil.copy2(cover_letter_path, job_dir / "cover_letter_used.pdf")

    snapshot_target = job_dir / "snapshot.png"
    if snapshot_path is not None and snapshot_path.is_file():
        if snapshot_path.resolve() != snapshot_target.resolve():
            shutil.copy2(snapshot_path, snapshot_target)
    if page_source and not snapshot_target.is_file():
        (job_dir / "page_source.html").write_text(page_source, encoding="utf-8")

    if not snapshot_target.is_file() and not (job_dir / "page_source.html").is_file():
        raise FileNotFoundError(f"No snapshot or page source for {details.job_id}")

    payload = details.model_dump()
    (job_dir / "job_details.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for name in ("cv_used.pdf", "cover_letter_used.pdf", "job_details.json"):
        written = job_dir / name
        if not written.is_file() or written.stat().st_size == 0:
            raise OSError(f"Failed to write {written}")
    _remember(paths, details)
    logger.info("Recorded application %s", job_dir)
    return job_dir


def _load_index(paths: ProjectPaths) -> list[dict[str, str]]:
    path = paths.application_index
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read %s", path)
        return []
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return [item for item in jobs if isinstance(item, dict)]


def _remember(paths: ProjectPaths, details: JobDetails) -> None:
    jobs = [
        item
        for item in _load_index(paths)
        if item.get("job_id") != details.job_id and item.get("source_url") != details.source_url
    ]
    jobs.append(
        {
            "job_id": details.job_id,
            "source_url": details.source_url,
            "job_title": details.job_title,
            "applied_timestamp": details.applied_timestamp,
        }
    )
    paths.applications.mkdir(parents=True, exist_ok=True)
    paths.application_index.write_text(json.dumps({"jobs": jobs}, indent=2) + "\n", encoding="utf-8")
    if not paths.application_index.is_file():
        raise OSError(f"Failed to write {paths.application_index}")
