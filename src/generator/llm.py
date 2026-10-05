"""Optional OpenAI-compatible chat client used to synthesize profile text."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("jobhunter.llm")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


class LLMClient:
    """Call a chat-completions endpoint when an API key is configured.

    With no key, ``complete`` returns None and the pipeline keeps the
    text extracted directly from the base documents.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        if api_key is None:
            api_key = os.getenv("JOBHUNTER_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        if base_url is None:
            base_url = os.getenv("JOBHUNTER_LLM_BASE_URL") or "https://api.openai.com/v1"
        if model is None:
            model = os.getenv("JOBHUNTER_LLM_MODEL") or "gpt-4o-mini"
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or "gpt-4o-mini"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str) -> str | None:
        if not self.enabled:
            logger.info("LLM client is disabled; using document text only")
            return None
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user[:24_000]},
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            logger.exception("LLM request failed")
            return None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.error("Unexpected LLM response shape")
            return None
        if not isinstance(content, str) or not content.strip():
            return None
        return content

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        raw = self.complete(system, user)
        if raw is None:
            return None
        return extract_json(raw)


def extract_json(text: str) -> dict[str, Any] | None:
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if match is None:
            logger.error("LLM response was not JSON")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.error("LLM response contained invalid JSON")
            return None
    if isinstance(data, dict):
        return data
    logger.error("LLM JSON root was not an object")
    return None
