"""Verified, privacy-conscious browser control for JARVIS Mark II.

The browser runs in a dedicated profile and on one worker thread.  Public
methods return success only after inspecting a post-action browser state.
"""

from __future__ import annotations

import atexit
import ctypes
import importlib.util
import os
import queue
import re
import threading
import urllib.parse
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SECRET_ENV_MARKERS = ("api_key", "apikey", "secret", "token", "password", "credential")
_ROLE_NAMES = {
    "button", "checkbox", "combobox", "dialog", "heading", "link", "listbox",
    "menuitem", "option", "radio", "searchbox", "slider", "spinbutton", "switch",
    "tab", "textbox", "treeitem",
}


@dataclass(frozen=True)
class BrowserDescriptor:
    prog_id: str
    executable: Path
    engine: str
    source: str

    @property
    def automatable(self) -> bool:
        return self.engine == "chromium" and self.executable.is_file()

    @property
    def display_name(self) -> str:
        names = {
            "brave": "Brave",
            "chrome": "Google Chrome",
            "msedge": "Microsoft Edge",
            "vivaldi": "Vivaldi",
            "opera": "Opera",
            "opera_gx": "Opera GX",
            "chromium": "Chromium",
            "firefox": "Firefox",
        }
        return names.get(self.executable.stem.casefold(), self.executable.stem or "unknown")


class BrowserControl:
    """Own a Playwright persistent context without sharing the user's profile."""

    def __init__(self, state_dir: Path, executable_path: str = "", engine: str = "") -> None:
        self.state_dir = Path(state_dir)
        self._explicit_executable = str(executable_path or "").strip()
        self._explicit_engine = str(engine or "").strip().casefold()
        self.browser = self._resolve_browser()
        self.profile_dir = self._profile_for(self.browser)
        self._commands: queue.Queue[tuple[str, dict[str, Any], Future[str]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._started = threading.Event()
        self._startup_error = ""
        self._session_active = False
        atexit.register(self.close)

    @staticmethod
    def _parse_executable(command: str) -> Path:
        expanded = os.path.expandvars(str(command or "").strip())
        if not expanded:
            return Path()
        if expanded.startswith('"'):
            end = expanded.find('"', 1)
            if end > 1:
                return Path(expanded[1:end])
        match = re.match(r"(?i)^(.+?\.exe)(?:\s|$)", expanded)
        return Path(match.group(1).strip()) if match else Path()

    @staticmethod
    def _engine_for(executable: Path, prog_id: str = "") -> str:
        identity = f"{executable.stem} {prog_id}".casefold()
        if any(name in identity for name in (
            "chrome", "chromium", "msedge", "brave", "vivaldi", "opera", "arc",
        )):
            return "chromium"
        if "firefox" in identity:
            # Stock Firefox is not compatible with Playwright's patched Firefox
            # transport. Keep detection truthful instead of pretending CDP works.
            return "firefox-system"
        return "unsupported"

    @staticmethod
    def _registry_default() -> tuple[str, Path] | None:
        if os.name != "nt":
            return None
        try:
            import winreg

            choice = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, choice) as key:
                prog_id = str(winreg.QueryValueEx(key, "ProgId")[0] or "").strip()
            if not prog_id:
                return None
            key_paths = (
                (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell\open\command"),
                (winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"),
            )
            for hive, key_path in key_paths:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        command = str(winreg.QueryValueEx(key, "")[0] or "")
                    executable = BrowserControl._parse_executable(command)
                    if executable.name:
                        return prog_id, executable
                except OSError:
                    continue
        except OSError:
            return None
        return None

    @staticmethod
    def _association_executable() -> Path:
        if os.name != "nt":
            return Path()
        try:
            from ctypes import wintypes

            query = ctypes.windll.shlwapi.AssocQueryStringW
            length = wintypes.DWORD(0)
            query(0, 2, "https", None, None, ctypes.byref(length))
            if not length.value:
                return Path()
            buffer = ctypes.create_unicode_buffer(length.value)
            result = query(0, 2, "https", None, buffer, ctypes.byref(length))
            path = Path(buffer.value) if result == 0 else Path()
            if path.name.casefold() in {"openwith.exe", "rundll32.exe", "applicationframehost.exe"}:
                return Path()
            return path
        except (AttributeError, OSError, ValueError):
            return Path()

    def _resolve_browser(self) -> BrowserDescriptor:
        configured = self._explicit_executable or str(
            os.environ.get("JARVIS_BROWSER_EXECUTABLE") or ""
        ).strip()
        if configured:
            executable = Path(os.path.expandvars(configured))
            engine = self._explicit_engine or self._engine_for(executable, "override")
            return BrowserDescriptor("override", executable, engine, "explicit-override")
        registry = self._registry_default()
        if registry:
            prog_id, executable = registry
            return BrowserDescriptor(
                prog_id, executable, self._engine_for(executable, prog_id), "windows-userchoice",
            )
        executable = self._association_executable()
        return BrowserDescriptor(
            "association", executable, self._engine_for(executable, "association"),
            "windows-association" if executable.name else "not-configured",
        )

    def _refresh_browser(self) -> None:
        if self._session_active:
            return
        self.browser = self._resolve_browser()
        self.profile_dir = self._profile_for(self.browser)

    def _adopt_default_change(self) -> None:
        """Switch cleanly when Windows' default changed between commands."""
        latest = self._resolve_browser()
        current_identity = (self.browser.prog_id.casefold(), str(self.browser.executable).casefold())
        latest_identity = (latest.prog_id.casefold(), str(latest.executable).casefold())
        if current_identity == latest_identity:
            return
        if self._session_active:
            self.close()
        self.browser = latest
        self.profile_dir = self._profile_for(latest)

    def _profile_for(self, browser: BrowserDescriptor) -> Path:
        identity = re.sub(r"[^a-z0-9]+", "-", browser.executable.stem.casefold()).strip("-")
        return self.state_dir / "browser-profiles" / (identity or "unknown")

    @staticmethod
    def _browser_env() -> dict[str, str]:
        """Do not expose JARVIS provider credentials to browser processes."""
        safe: dict[str, str] = {}
        for key, value in os.environ.items():
            normalized = key.casefold()
            if any(marker in normalized for marker in _SECRET_ENV_MARKERS):
                continue
            safe[key] = value
        return safe

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        if parsed.scheme.casefold() not in {"http", "https"}:
            raise ValueError("browser URLs must use http or https")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("browser URL must have a hostname and no embedded credentials")
        return urllib.parse.urlunsplit(parsed)

    @classmethod
    def normalize_target(cls, target: str) -> str:
        value = str(target or "").strip()
        if not value:
            return "https://www.google.com/"
        if re.match(r"^https?://", value, re.IGNORECASE):
            return cls._validate_url(value)
        if re.fullmatch(r"(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?::\d+)?(?:/[^\s]*)?", value, re.IGNORECASE):
            return cls._validate_url("https://" + value)
        if len(value) > 500:
            raise ValueError("browser search query is too long")
        return "https://www.google.com/search?q=" + urllib.parse.quote_plus(value)

    @staticmethod
    def first_watch_url(base_url: str, hrefs: list[str]) -> str:
        """Choose a normal YouTube watch result, never a Shorts route."""
        for href in hrefs:
            candidate = str(href or "").strip()
            if not candidate or "/shorts/" in candidate.casefold():
                continue
            absolute = urllib.parse.urljoin(base_url, candidate)
            parsed = urllib.parse.urlsplit(absolute)
            if parsed.hostname and parsed.hostname.casefold().endswith("youtube.com") and parsed.path == "/watch":
                video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
                if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                    return f"https://www.youtube.com/watch?v={video_id}"
        raise RuntimeError("no normal YouTube video result was available")

    def status(self) -> str:
        self._refresh_browser()
        dependency = importlib.util.find_spec("playwright") is not None
        executable = self.browser.executable.is_file()
        ready = dependency and self.browser.automatable
        system_default = self.browser.source != "explicit-override"
        return (
            "Browser Control Engine Phase 1 | "
            f"ready={str(ready).lower()} | playwright={str(dependency).lower()} | "
            f"browser={self.browser.display_name if executable else 'missing'} | "
            f"system_default={str(system_default).lower()} | association={self.browser.prog_id or 'unknown'} | "
            f"association_source={self.browser.source} | engine={self.browser.engine} | "
            f"session_active={str(self._session_active).lower()} | dedicated_profile=true | "
            f"secrets_in_browser_env=false | semantic_dom={str(ready).lower()} | "
            f"verified_postconditions={str(ready).lower()} | "
            "youtube_playback_probe=true"
        )

    def _ensure_worker(self) -> None:
        self._refresh_browser()
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._started.clear()
            self._startup_error = ""
            self._thread = threading.Thread(target=self._worker, name="jarvis-browser", daemon=True)
            self._thread.start()
        if not self._started.wait(15):
            raise RuntimeError("browser worker did not start")
        if self._startup_error:
            raise RuntimeError(self._startup_error)

    def _submit(self, command: str, timeout: float = 45.0, **kwargs: Any) -> str:
        self._ensure_worker()
        future: Future[str] = Future()
        self._commands.put((command, kwargs, future))
        try:
            return future.result(timeout=max(1.0, timeout))
        except FutureTimeout as exc:
            raise RuntimeError(f"browser command timed out after {timeout:g} seconds") from exc

    def _worker(self) -> None:
        playwright = None
        context = None
        try:
            if importlib.util.find_spec("playwright") is None:
                raise RuntimeError("Playwright is not installed in the JARVIS environment")
            if not self.browser.executable.is_file():
                raise RuntimeError("Windows does not have a resolvable default HTTPS browser")
            if not self.browser.automatable:
                raise RuntimeError(
                    f"system default {self.browser.display_name} was detected, but its engine "
                    "does not expose a compatible verified semantic automation transport"
                )
            from playwright.sync_api import sync_playwright

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                executable_path=str(self.browser.executable),
                headless=False,
                no_viewport=True,
                env=self._browser_env(),
                args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
            )
            self._session_active = True
        except Exception as exc:
            self._startup_error = f"browser startup failed: {type(exc).__name__}: {exc}"
        finally:
            self._started.set()

        if self._startup_error:
            return
        try:
            while True:
                command, kwargs, future = self._commands.get()
                if command == "close":
                    future.set_result("closed")
                    break
                try:
                    page = self._current_page(context)
                    handler = getattr(self, f"_command_{command}")
                    result = handler(page, **kwargs)
                except Exception as exc:
                    result = f"error: browser {command} failed: {type(exc).__name__}: {exc}"
                if not future.done():
                    future.set_result(result)
        finally:
            self._session_active = False
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass

    @staticmethod
    def _current_page(context: Any) -> Any:
        pages = [page for page in context.pages if not page.is_closed()]
        return pages[-1] if pages else context.new_page()

    def navigate(self, target: str = "") -> str:
        self._adopt_default_change()
        if not self.browser.automatable:
            url = self.normalize_target(target)
            if os.name == "nt" and self.browser.executable.is_file():
                os.startfile(url)
                return (
                    f"unverified: opened {url} with system default {self.browser.display_name}, "
                    "but this browser engine has no compatible semantic verification transport; "
                    "use Windows vision as a fallback and do not claim navigation success yet"
                )
        return self._submit("navigate", target=target, timeout=45)

    def _command_navigate(self, page: Any, target: str = "") -> str:
        url = self.normalize_target(target)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(350)
        actual = self._validate_url(page.url)
        title = str(page.title() or "").strip()[:200]
        return f"Verified browser navigation | url={actual[:1000]} | title={title or '(untitled)'}"

    def page_state(self, max_chars: int = 6000) -> str:
        if not self._session_active:
            return "error: no active JARVIS browser session; navigate first"
        return self._submit("page_state", max_chars=max_chars, timeout=20)

    def _command_page_state(self, page: Any, max_chars: int = 6000) -> str:
        limit = max(200, min(int(max_chars), 20_000))
        url = self._validate_url(page.url)
        title = str(page.title() or "").strip()[:200]
        text = str(page.locator("body").inner_text(timeout=8_000) or "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]
        return f"Browser page state | url={url[:1000]} | title={title or '(untitled)'}\n{text or '(no visible body text)'}"

    @staticmethod
    def _candidate_locators(page: Any, target: str, role: str, action: str) -> list[Any]:
        candidates: list[Any] = []
        clean_role = str(role or "").strip().casefold()
        if clean_role:
            if clean_role not in _ROLE_NAMES:
                raise ValueError(f"unsupported browser role: {role}")
            candidates.append(page.get_by_role(clean_role, name=target, exact=True))
        if action in {"fill", "select", "check", "uncheck"}:
            candidates.extend([
                page.get_by_label(target, exact=True),
                page.get_by_placeholder(target, exact=True),
            ])
        candidates.append(page.get_by_text(target, exact=True))
        return candidates

    @classmethod
    def _resolve_locator(cls, page: Any, target: str, role: str, action: str, occurrence: int | None) -> Any:
        if not str(target or "").strip():
            raise ValueError("browser target cannot be empty")
        for candidate in cls._candidate_locators(page, target, role, action):
            try:
                visible = [candidate.nth(index) for index in range(min(candidate.count(), 51)) if candidate.nth(index).is_visible()]
            except Exception:
                continue
            if not visible:
                continue
            if occurrence is None and len(visible) > 1:
                raise RuntimeError(
                    f"target is ambiguous ({len(visible)} visible matches); provide occurrence"
                )
            index = int(occurrence or 0)
            if index < 0 or index >= len(visible):
                raise RuntimeError(f"occurrence {index} is outside {len(visible)} visible matches")
            return visible[index]
        raise RuntimeError("no visible semantic target matched")

    @staticmethod
    def _media_probe(page: Any, wait_seconds: float = 0.9) -> tuple[bool, str]:
        script = """video => ({paused: video.paused, ended: video.ended,
            readyState: video.readyState, currentTime: video.currentTime,
            duration: Number.isFinite(video.duration) ? video.duration : null})"""
        before = page.locator("video").first.evaluate(script)
        page.wait_for_timeout(max(100, int(wait_seconds * 1000)))
        after = page.locator("video").first.evaluate(script)
        advanced = float(after.get("currentTime") or 0) > float(before.get("currentTime") or 0) + 0.15
        playing = not bool(after.get("paused")) and not bool(after.get("ended")) and int(after.get("readyState") or 0) >= 2 and advanced
        evidence = (
            f"paused={str(bool(after.get('paused'))).lower()},ready={int(after.get('readyState') or 0)},"
            f"advanced={str(advanced).lower()}"
        )
        return playing, evidence

    @staticmethod
    def _verify_postconditions(
        page: Any,
        expected_url_contains: str,
        expected_text: str,
        expected_media_playing: bool,
    ) -> str:
        checks: list[str] = []
        if expected_url_contains:
            if expected_url_contains.casefold() not in str(page.url).casefold():
                raise RuntimeError("expected URL fragment was not observed")
            checks.append("url")
        if expected_text:
            page.get_by_text(expected_text, exact=False).first.wait_for(state="visible", timeout=8_000)
            checks.append("text")
        if expected_media_playing:
            page.locator("video").first.wait_for(state="visible", timeout=8_000)
            playing, evidence = BrowserControl._media_probe(page)
            if not playing:
                raise RuntimeError(f"media playback was not proven ({evidence})")
            checks.append("media-progress")
        return ",".join(checks)

    def interact(
        self,
        action: str,
        target: str,
        value: str = "",
        role: str = "",
        occurrence: int | None = None,
        expected_url_contains: str = "",
        expected_text: str = "",
        expected_media_playing: bool = False,
    ) -> str:
        return self._submit(
            "interact", timeout=45, action=action, target=target, value=value, role=role,
            occurrence=occurrence, expected_url_contains=expected_url_contains,
            expected_text=expected_text, expected_media_playing=expected_media_playing,
        )

    def _command_interact(self, page: Any, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "").casefold()
        if action not in {"click", "fill", "press", "select", "check", "uncheck"}:
            raise ValueError(f"unsupported browser action: {action}")
        expected_url = str(kwargs.get("expected_url_contains") or "").strip()
        expected_text = str(kwargs.get("expected_text") or "").strip()
        expected_media = bool(kwargs.get("expected_media_playing"))
        if not (expected_url or expected_text or expected_media):
            raise ValueError("a browser interaction requires an explicit expected postcondition")
        locator = self._resolve_locator(
            page, str(kwargs.get("target") or ""), str(kwargs.get("role") or ""),
            action, kwargs.get("occurrence"),
        )
        value = str(kwargs.get("value") or "")
        if action == "click":
            locator.click(timeout=8_000)
        elif action == "fill":
            locator.fill(value, timeout=8_000)
        elif action == "press":
            locator.press(value, timeout=8_000)
        elif action == "select":
            locator.select_option(value, timeout=8_000)
        elif action == "check":
            locator.check(timeout=8_000)
        else:
            locator.uncheck(timeout=8_000)
        evidence = self._verify_postconditions(page, expected_url, expected_text, expected_media)
        return f"Verified browser interaction | action={action} | evidence={evidence}"

    def play_youtube(self, query: str) -> str:
        self._adopt_default_change()
        if not self.browser.automatable:
            clean = str(query or "").strip()
            if len(clean) < 2 or len(clean) > 300:
                raise ValueError("YouTube query must be between 2 and 300 characters")
            url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(clean)
            if os.name == "nt" and self.browser.executable.is_file():
                os.startfile(url)
                return (
                    f"unverified: opened YouTube search in system default {self.browser.display_name}, "
                    "but playback was not semantically controllable or proven; use Windows vision fallback"
                )
        return self._submit("play_youtube", query=query, timeout=70)

    def _command_play_youtube(self, page: Any, query: str) -> str:
        clean = str(query or "").strip()
        if len(clean) < 2 or len(clean) > 300:
            raise ValueError("YouTube query must be between 2 and 300 characters")
        search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(clean)
        page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        links = page.locator("ytd-video-renderer a#video-title")
        links.first.wait_for(state="attached", timeout=15_000)
        hrefs = [links.nth(index).get_attribute("href") or "" for index in range(min(links.count(), 30))]
        watch_url = self.first_watch_url(page.url, hrefs)
        page.goto(watch_url, wait_until="domcontentloaded", timeout=30_000)
        video = page.locator("video").first
        video.wait_for(state="visible", timeout=15_000)
        state = video.evaluate("v => ({paused: v.paused, readyState: v.readyState})")
        if bool(state.get("paused")):
            try:
                page.get_by_role("button", name=re.compile(r"^(play|resume)$", re.I)).first.click(timeout=3_000)
            except Exception:
                video.click(timeout=5_000)
        playing, evidence = self._media_probe(page, wait_seconds=1.2)
        if not playing:
            raise RuntimeError(f"YouTube opened but verified playback did not start ({evidence})")
        title = str(page.title() or "").removesuffix(" - YouTube").strip()[:200]
        return f"Verified YouTube playback | title={title or '(untitled)'} | url={watch_url} | {evidence}"

    def close(self) -> None:
        thread = self._thread
        if not thread or not thread.is_alive():
            return
        future: Future[str] = Future()
        self._commands.put(("close", {}, future))
        try:
            future.result(timeout=3)
            thread.join(timeout=5)
        except Exception:
            pass
