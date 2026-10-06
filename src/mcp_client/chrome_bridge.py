"""Chrome MCP client used for navigation, form filling, uploads, and screenshots."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from src.exceptions import ChromeInteractionError, ChromeNotConfigured

logger = logging.getLogger("jobhunter.chrome")

ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "navigate": ("navigate", "browser_navigate", "goto", "open"),
    "click": ("click", "browser_click"),
    "fill": ("fill", "fill_input", "browser_fill", "browser_type", "type"),
    "screenshot": ("screenshot", "take_screenshot", "browser_take_screenshot", "browser_screenshot"),
    "get_html": (
        "get_html",
        "get_page_content",
        "browser_get_html",
        "get_content",
        "browser_snapshot",
        "snapshot",
    ),
    "upload": ("upload_file", "browser_file_upload", "set_input_files", "upload"),
    "evaluate": ("browser_evaluate", "evaluate"),
    "tabs": ("browser_tabs", "tabs"),
    "run_code": ("browser_run_code_unsafe", "run_code"),
}

_SCHEMA_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("url", ("url", "uri", "href")),
    ("paths", ("paths", "files")),
    ("path", ("path", "filename", "file")),
    ("value", ("value", "text", "content")),
    ("selector", ("target", "selector", "query", "css")),
    ("description", ("description", "element", "intent")),
    ("function", ("function",)),
    ("action", ("action",)),
    ("index", ("index",)),
    ("code", ("code",)),
)


class BrowserSession(Protocol):
    """Actions the pipeline expects from a live or scripted browser."""

    async def navigate(self, url: str) -> str: ...

    async def click(self, *, selector: str = "", description: str = "") -> str: ...

    async def fill(self, *, selector: str = "", value: str, description: str = "") -> str: ...

    async def screenshot(self, path: Path) -> Path: ...

    async def get_html(self) -> str: ...

    async def upload(
        self,
        paths: list[Path],
        *,
        selector: str = "",
        description: str = "",
    ) -> str: ...


class ChromeBridge:
    """Launch a Chrome MCP server and expose the browser actions above.

    Set ``JOBHUNTER_CHROME_MCP_COMMAND`` to the server launch command, for example
    ``npx -y @playwright/mcp@latest``. Tool names from the spec (``navigate``,
    ``click``, ``fill``, ``screenshot``, ``get_html``) are mapped onto the names
    the connected server actually exposes.
    """

    def __init__(self, command: str | None = None) -> None:
        if command is None:
            command = os.getenv("JOBHUNTER_CHROME_MCP_COMMAND", "")
        self.command = command.strip()

    @property
    def configured(self) -> bool:
        return bool(self.command)

    @asynccontextmanager
    async def session(
        self,
        profile_directory: str | None = None,
        *,
        incognito: bool = False,
        user_data_dir: Path | None = None,
        config_path: Path | None = None,
    ) -> AsyncIterator[BrowserSession]:
        if not self.configured:
            raise ChromeNotConfigured(
                "Set JOBHUNTER_CHROME_MCP_COMMAND to the Chrome MCP server launch command."
            )
        client_session, server_parameters, stdio_client = _load_mcp()
        if incognito or profile_directory:
            launch = command_for_browser(
                self.command,
                incognito=incognito,
                directory=profile_directory or "",
                user_data_dir=user_data_dir,
                config_path=config_path,
            )
        else:
            launch = self.command
        command, args = _split_command(launch)
        params = server_parameters(command=command, args=args)
        try:
            async with stdio_client(params) as streams:
                read, write = streams
                async with client_session(read, write) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    tools = getattr(listed, "tools", listed)
                    yield _ChromeSession(client, tools, _timeout_seconds())
        except ChromeNotConfigured:
            raise
        except ChromeInteractionError:
            raise
        except Exception as exc:
            raise ChromeInteractionError(f"Chrome MCP session failed: {exc}") from exc


class _ChromeSession:
    def __init__(self, client: object, tools: object, timeout: float) -> None:
        self._client = client
        self._timeout = timeout
        self._tools: dict[str, object] = {}
        self._schemas: dict[str, dict[str, object]] = {}
        for tool in tools or []:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
            self._tools[name] = tool
            self._schemas[name] = schema if isinstance(schema, dict) else {}
        logger.info("Chrome MCP tools: %s", ", ".join(sorted(self._tools)) or "(none)")

    async def navigate(self, url: str) -> str:
        result = await self._call("navigate", {"url": url, "description": url})
        return _result_text(result) or url

    async def click(self, *, selector: str = "", description: str = "") -> str:
        label = description or selector or "button"
        target = await self._target_for(label, fallback=selector)
        result = await self._call("click", {"selector": target, "description": label})
        return _result_text(result) or "clicked"

    async def fill(self, *, selector: str = "", value: str, description: str = "") -> str:
        secret = "password" in f"{selector} {description}".lower()
        label = description or selector or "input"
        logger.info("Fill %s", label)
        if not secret:
            logger.debug("Fill value length %s", len(value))
        target = await self._target_for(label, fallback=selector)
        result = await self._call(
            "fill",
            {"selector": target, "value": value, "description": label},
        )
        return _result_text(result) or "filled"

    async def get_html(self) -> str:
        if self._resolve_optional("evaluate"):
            try:
                result = await self._call(
                    "evaluate",
                    {
                        "function": "() => document.documentElement.outerHTML",
                        "description": "page html",
                    },
                )
                text = _unwrap_eval(_result_text(result))
                if "<" in text and ("<a " in text.lower() or "<html" in text.lower() or "<body" in text.lower()):
                    return text
            except ChromeInteractionError:
                logger.info("Page HTML was not returned; using the accessibility snapshot")
        result = await self._call("get_html", {"description": "current page"})
        text = _result_text(result)
        if not text.strip():
            raise ChromeInteractionError("Chrome MCP returned empty page content")
        return text

    async def _target_for(self, description: str, *, fallback: str) -> str:
        """Use a snapshot ref when the page has a control matching this description."""
        try:
            result = await self._call("get_html", {"description": "page snapshot"})
        except ChromeInteractionError:
            logger.info("No page snapshot for %s", description)
            return fallback
        ref = snapshot_target(_result_text(result), description)
        if ref:
            logger.info("Using snapshot target %s for %s", ref, description)
            return ref
        return fallback

    async def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = await self._call(
            "screenshot",
            {"path": str(path), "filename": path.name, "description": "page screenshot"},
        )
        if _write_image(result, path):
            return path
        if path.is_file() and path.stat().st_size > 0:
            return path
        for token in _result_text(result).split():
            candidate = Path(token.strip("\"'"))
            if candidate.is_file():
                if candidate.resolve() != path.resolve():
                    shutil.copyfile(candidate, path)
                return path
        raise ChromeInteractionError("Chrome MCP did not return a screenshot")

    async def upload(
        self,
        paths: list[Path],
        *,
        selector: str = "",
        description: str = "",
    ) -> str:
        absolute: list[str] = []
        for path in paths:
            if not path.is_file():
                raise ChromeInteractionError(f"Upload file does not exist: {path}")
            absolute.append(str(path.resolve()))
        del selector, description
        await self._dismiss_file_chooser()
        result = await self._call("run_code", {"code": file_input_script(Path(absolute[0]))})
        placed = _unwrap_eval(_result_text(result)).strip()
        logger.info("File input result: %s", placed[:200])
        if placed == "no-file-input":
            raise ChromeInteractionError(
                "This page has no file field yet. Open the application form so the CV field is visible, "
                "then choose the file again. Leave the Playwright bar up, and do not click the site's upload button."
            )
        if not placed.startswith("set:"):
            raise ChromeInteractionError(
                "The page did not take the file. Open the application form, leave the Playwright bar up, "
                "and choose the file again without clicking the site's upload button."
            )
        return placed

    async def _dismiss_file_chooser(self) -> None:
        """Drop a file dialog Playwright already captured. Supplying files that way hangs on real Chrome."""
        if not self._resolve_optional("upload"):
            return
        try:
            await self._call("upload", {}, timeout=8)
        except ChromeInteractionError as exc:
            logger.info("No file dialog was waiting: %s", exc)

    async def focus_site(self, site_urls: list[str]) -> str:
        """Read the Chrome tab in front. Do not switch to a different job tab."""
        if not self._resolve_optional("tabs"):
            raise ChromeInteractionError("Chrome MCP cannot list open tabs.")
        result = await self._call("tabs", {"action": "list", "description": "open tabs"})
        listing = _result_text(result)
        tabs = parse_browser_tabs(listing)
        logger.info("Open tabs: %s", listing.replace("\n", " | ")[:500])
        visible_url = await self._visible_page_url()
        if visible_url:
            logger.info("Focused page: %s", visible_url)
        chosen = choose_focused_tab(tabs, visible_url)
        if chosen is None:
            allowed = ", ".join(url for url in site_urls if url.strip()) or "sites.csv"
            raise ChromeInteractionError(
                f"The tab in front is not a job page. Click the job on {allowed} so that page is in front, "
                "then read it again. The Playwright welcome page is not a job."
            )
        if not chosen.current:
            await self._call(
                "tabs",
                {"action": "select", "index": chosen.index, "description": chosen.url},
            )
        logger.info("Reading focused tab %s: %s", chosen.index, chosen.url)
        return chosen.url

    async def _visible_page_url(self) -> str:
        """URL of the Chrome tab the person is looking at, without bringing another tab forward."""
        if not self._resolve_optional("run_code"):
            return ""
        try:
            result = await self._call("run_code", {"code": _FOCUSED_PAGE_CODE})
        except ChromeInteractionError:
            logger.info("Could not check which Chrome tab is in front")
            return ""
        rows = _parse_focus_rows(_result_text(result))
        focused = [
            row
            for row in rows
            if row.get("focused") and not is_extension_tab(str(row.get("url") or ""))
        ]
        if len(focused) == 1:
            return str(focused[0].get("url") or "")
        visible = [
            row
            for row in rows
            if row.get("visibility") == "visible" and not is_extension_tab(str(row.get("url") or ""))
        ]
        if len(visible) == 1:
            return str(visible[0].get("url") or "")
        return ""

    async def _call(self, action: str, arguments: dict[str, object], timeout: float | None = None) -> object:
        tool_name = self._resolve(action)
        payload = adapt_arguments(self._schemas.get(tool_name, {}), arguments)
        logger.info("Chrome MCP %s via %s", action, tool_name)
        logger.debug("Chrome MCP payload keys: %s", sorted(payload))
        call_tool = getattr(self._client, "call_tool")
        try:
            result = await asyncio.wait_for(call_tool(tool_name, payload), timeout=timeout or self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ChromeInteractionError(f"{action} failed: {exc}") from exc
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            detail = _result_text(result) or "tool error"
            raise ChromeInteractionError(f"{action} failed: {detail}")
        return result

    def _resolve(self, action: str) -> str:
        resolved = self._resolve_optional(action)
        if resolved:
            return resolved
        available = ", ".join(sorted(self._tools)) or "(none)"
        raise ChromeInteractionError(
            f"Chrome MCP server has no tool for '{action}'. Available tools: {available}"
        )

    def _resolve_optional(self, action: str) -> str:
        for alias in ACTION_ALIASES.get(action, (action,)):
            if alias in self._tools:
                return alias
        return ""


_SNAPSHOT_NODE = re.compile(
    r'(?P<role>textbox|searchbox|combobox|button|link)\b(?:\s+"(?P<name>[^"]*)")?[^\[\n]*\[ref=(?P<ref>[A-Za-z0-9]+)\]',
    re.I,
)
_STOP_WORDS = {"the", "a", "an", "box", "field", "input", "button", "on", "for"}


def snapshot_target(snapshot: str, description: str) -> str | None:
    """Pick a Playwright ref for a search, location, or apply control."""
    nodes = [
        (match.group("role").lower(), (match.group("name") or "").strip(), match.group("ref"))
        for match in _SNAPSHOT_NODE.finditer(snapshot)
    ]
    if not nodes:
        return None
    desc = description.lower()
    if "location" in desc:
        return _best_ref(
            nodes,
            {"textbox", "searchbox", "combobox"},
            ("where", "location", "city", "suburb", "地區", "地点", "位置"),
            avoid=("what", "keyword", "job title", "company"),
        )
    if "search box" in desc or "job search" in desc:
        return _best_ref(
            nodes,
            {"textbox", "searchbox", "combobox"},
            ("what", "keyword", "job title", "search", "職位", "关键字", "關鍵字"),
            avoid=("where", "location", "city", "suburb"),
        )
    if desc.strip() in {"search", "search button"}:
        return _best_ref(
            nodes,
            {"button"},
            ("search", "seek", "find jobs", "find", "搜尋", "搜索"),
            avoid=(),
        )
    words = tuple(word for word in re.findall(r"[a-z0-9]+", desc) if len(word) > 2 and word not in _STOP_WORDS)
    if not words:
        return None
    return _best_ref(nodes, {"button", "link", "textbox", "searchbox", "combobox"}, words, avoid=())


def _best_ref(
    nodes: list[tuple[str, str, str]],
    roles: set[str],
    hints: tuple[str, ...],
    *,
    avoid: tuple[str, ...],
) -> str | None:
    best_score = 0
    best_ref = ""
    for role, name, ref in nodes:
        if role not in roles:
            continue
        lowered = name.lower()
        if any(token in lowered for token in avoid):
            continue
        score = sum(2 for hint in hints if hint.lower() in lowered)
        if score > best_score:
            best_score = score
            best_ref = ref
    return best_ref or None


@dataclass(frozen=True)
class BrowserTab:
    index: int
    title: str
    url: str
    current: bool


_TAB_LINE = re.compile(
    r"^- (?P<index>\d+):(?P<current> \(current\))? \[(?P<title>.*?)\]\((?P<url>[^)]+)\)",
    re.M,
)


def parse_browser_tabs(text: str) -> list[BrowserTab]:
    """Read Playwright's tab list. Each line is `- 0: (current) [Title](url)`."""
    tabs: list[BrowserTab] = []
    for match in _TAB_LINE.finditer(text):
        tabs.append(
            BrowserTab(
                index=int(match.group("index")),
                title=match.group("title").strip(),
                url=match.group("url").strip(),
                current=bool(match.group("current")),
            )
        )
    return tabs


_FOCUSED_PAGE_CODE = """async (page) => {
  const pages = page.context().pages();
  const rows = [];
  for (const candidate of pages) {
    let url = "";
    let visibility = "hidden";
    let focused = false;
    try {
      url = candidate.url();
      const state = await candidate.evaluate(() => ({
        visibility: document.visibilityState,
        focused: document.hasFocus(),
      }));
      visibility = state.visibility;
      focused = Boolean(state.focused);
    } catch (error) {
      visibility = "hidden";
    }
    rows.push({ url, visibility, focused });
  }
  return rows;
}"""


def choose_focused_tab(tabs: list[BrowserTab], visible_url: str = "") -> BrowserTab | None:
    """Return the tab in front. A different job tab is left alone."""
    pages = [tab for tab in tabs if not is_extension_tab(tab.url)]
    if visible_url.strip():
        wanted = _page_key(visible_url)
        for tab in pages:
            if _page_key(tab.url) == wanted:
                return tab
    for tab in pages:
        if tab.current:
            return tab
    return None


def is_extension_tab(url: str) -> bool:
    """The Playwright connect tab is not a page the person opened."""
    return urlparse(url).scheme.lower() == "chrome-extension"


def is_site_home(url: str) -> bool:
    """True for the site landing page, before a job post is open."""
    path = urlparse(url).path.strip("/").lower()
    return path in {"", "hk", "en", "au", "sg"}


def url_on_listed_site(url: str, site_urls: list[str]) -> bool:
    """True when the page host is a site from sites.csv, including a subdomain."""
    page_host = _host(url)
    if not page_host:
        return False
    for site_url in site_urls:
        site_host = _host(site_url)
        if not site_host:
            continue
        if page_host == site_host or page_host.endswith(f".{site_host}"):
            return True
    return False


_PLAYWRIGHT_LOG = re.compile(r"### Ran Playwright code\b.*", re.I | re.S)
_RESULT_HEADER = re.compile(r"^### [^\n]*\n")


def is_playwright_status_page(html: str) -> bool:
    """The extension connect tab says Welcome and that mcp is connected. A job post does not."""
    cleaned = _PLAYWRIGHT_LOG.sub(" ", html)
    visible = " ".join(re.sub(r"(?is)<[^>]+>", " ", cleaned).lower().split())
    return visible.startswith("welcome") and "mcp" in visible and "connected" in visible and len(visible) < 400


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _page_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    query = "&".join(sorted(part for part in parsed.query.split("&") if part))
    return f"{_host(url)}{path}?{query}"


def _parse_focus_rows(text: str) -> list[dict[str, object]]:
    body = _unwrap_eval(text).strip()
    if not body:
        return []
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return []
    if isinstance(loaded, str):
        try:
            loaded = json.loads(loaded)
        except json.JSONDecodeError:
            return []
    if not isinstance(loaded, list):
        return []
    return [row for row in loaded if isinstance(row, dict)]


def file_input_script(path: Path) -> str:
    """Place a local file on the page without Chrome's file window.

    Playwright's upload command asks real Chrome for a custom debugger call that
    never returns, so the CV field is filled from the page instead.
    """
    data = path.read_bytes()
    if len(data) > 8_000_000:
        raise ChromeInteractionError(f"{path.name} is too large to place while Playwright is attached.")
    payload = json.dumps(
        {
            "b64": base64.b64encode(data).decode("ascii"),
            "name": path.name,
            "type": _upload_mime(path),
        }
    )
    return f"""async (page) => {{
  const payload = {payload};
  const inputs = page.locator('input[type="file"]');
  const count = await inputs.count();
  if (!count) return "no-file-input";
  let index = 0;
  let best = -100;
  for (let i = 0; i < count; i++) {{
    const score = await inputs.nth(i).evaluate((node) => {{
      const blob = [node.name, node.id, node.accept, node.className, node.getAttribute("aria-label")].join(" ").toLowerCase();
      let value = 0;
      if (/cv|resume|curriculum|upload/.test(blob)) value += 2;
      if (/pdf|doc/.test(blob)) value += 1;
      if (node.disabled) value -= 5;
      return value;
    }});
    if (score > best) {{
      best = score;
      index = i;
    }}
  }}
  const placed = await inputs.nth(index).evaluate((node, payload) => {{
    const binary = atob(payload.b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const file = new File([bytes], payload.name, {{ type: payload.type }});
    const transfer = new DataTransfer();
    transfer.items.add(file);
    node.files = transfer.files;
    node.dispatchEvent(new Event("input", {{ bubbles: true }}));
    node.dispatchEvent(new Event("change", {{ bubbles: true }}));
    return node.files && node.files[0] ? node.files[0].name : "";
  }}, payload);
  return placed ? "set:" + placed : "empty";
}}"""


def _upload_mime(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".rtf": "application/rtf",
    }.get(path.suffix.lower(), "application/octet-stream")


def _unwrap_eval(text: str) -> str:
    """Drop Playwright's tool transcript and return the page HTML."""
    body = _PLAYWRIGHT_LOG.sub("", text).strip()
    body = _RESULT_HEADER.sub("", body).strip()
    if body.startswith('"'):
        try:
            loaded = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(loaded, str):
            return loaded
    return body


def adapt_arguments(schema: Mapping[str, object], arguments: Mapping[str, object]) -> dict[str, object]:
    """Map pipeline arguments onto the keys a connected MCP tool actually accepts."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return {key: value for key, value in arguments.items() if value not in ("", [], None)}

    payload: dict[str, object] = {}
    used: set[str] = set()
    for source, candidates in _SCHEMA_KEYS:
        if source not in arguments or arguments[source] in ("", [], None):
            continue
        for candidate in candidates:
            if candidate in properties and candidate not in used:
                payload[candidate] = arguments[source]
                used.add(candidate)
                break

    required = schema.get("required")
    missing = [name for name in required if name not in payload] if isinstance(required, list) else []
    if missing:
        raise ChromeInteractionError(
            "Chrome MCP tool requires "
            + ", ".join(str(name) for name in missing)
            + f". Accepted fields: {', '.join(str(name) for name in properties)}"
        )
    return payload


def _load_mcp() -> tuple[object, object, object]:
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        raise ChromeNotConfigured(
            "The mcp package is not installed. Run: .venv/bin/pip install -r requirements.txt"
        ) from exc
    return ClientSession, StdioServerParameters, stdio_client


def command_for_profile(command: str, directory: str) -> str:
    """Attach Playwright to the Chrome window already open for this profile."""
    return command_for_browser(command, incognito=False, directory=directory)


def command_for_browser(
    command: str,
    *,
    incognito: bool,
    directory: str = "",
    user_data_dir: Path | None = None,
    config_path: Path | None = None,
) -> str:
    """Build the Playwright MCP command.

    Incognito starts a fresh window. A numbered profile attaches to the Chrome
    window already running that profile and opens sites there.
    """
    del user_data_dir, config_path
    parts = shlex.split(command)
    cleaned: list[str] = []
    skip_next = False
    drop_flags = {"--extension", "--isolated", "--profile-dir-name", "--browser", "--config"}
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part in drop_flags:
            if part in {"--profile-dir-name", "--browser", "--config"}:
                skip_next = True
            continue
        if part.startswith(("--profile-dir-name=", "--browser=", "--config=")):
            continue
        cleaned.append(part)
    if incognito:
        cleaned.extend(["--isolated", "--browser", "chrome"])
    else:
        if not directory:
            raise ChromeNotConfigured("A Chrome profile launch needs the profile directory.")
        cleaned.extend(["--extension", f"--profile-dir-name={directory}"])
    return shlex.join(cleaned)


def _split_command(command: str) -> tuple[str, list[str]]:
    parts = shlex.split(command)
    if not parts:
        raise ChromeNotConfigured("Chrome MCP command is empty")
    return parts[0], parts[1:]


def _timeout_seconds() -> float:
    raw = os.getenv("JOBHUNTER_MCP_TIMEOUT", "45")
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 45.0


def _result_text(result: object) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _write_image(result: object, path: Path) -> bool:
    for block in getattr(result, "content", []) or []:
        data = getattr(block, "data", None)
        mime = str(getattr(block, "mimeType", None) or getattr(block, "mime_type", "") or "")
        block_type = str(getattr(block, "type", "") or "")
        if not data or (block_type != "image" and not mime.startswith("image/")):
            continue
        try:
            raw = base64.b64decode(data)
        except (ValueError, TypeError) as exc:
            raise ChromeInteractionError("Chrome MCP returned an unreadable screenshot") from exc
        path.write_bytes(raw)
        return path.is_file() and path.stat().st_size > 0
    return False
