"""Project paths and defensive directory creation."""

from __future__ import annotations

from pathlib import Path

from src.textutil import slugify


class ProjectPaths:
    """Root-relative locations the pipeline is allowed to read and write."""

    def __init__(
        self,
        root: Path,
        *,
        docs: Path | None = None,
        sites: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.docs = (docs or self.root / "docs").resolve()
        self.generated_docs = self.root / "generated_docs"
        self.applications = self.root / "applications"
        self.sites = (sites or self.root / "sites.csv").resolve()
        self.profile = self.generated_docs / "profile.json"
        self.application_index = self.applications / "index.json"
        self.logs = self.root / "logs" / "jobhunter.log"

    def ensure(self) -> None:
        for folder in (self.docs, self.generated_docs, self.applications, self.logs.parent):
            folder.mkdir(parents=True, exist_ok=True)

    def specialization_dir(self, area: str) -> Path:
        slug = slugify(area, fallback="specialization", limit=60)
        folder = self.generated_docs / slug
        folder.relative_to(self.generated_docs)
        return folder

    def application_dir(self, job_id: str) -> Path:
        slug = slugify(job_id, fallback="job", limit=80)
        folder = self.applications / slug
        folder.resolve().relative_to(self.applications.resolve())
        return folder
