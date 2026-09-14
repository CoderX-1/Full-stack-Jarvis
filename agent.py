#!/usr/bin/env python3
"""Small local agent runner for OpenAI and OpenAI-compatible APIs.

The fullstack-agent repository is an AI-operated installer.  This runner gives
that installer (and the resulting assistant) the file and command tools it
needs without depending on a vendor-specific desktop subscription or SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from jarvis_mark2 import MARK2_TOOL_NAMES, MARK2_TOOLS, READ_ONLY_MARK2_TOOLS, Mark2Runtime

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENAI_BASE_URL = "https://api.openai.com/v1"
MAX_TOOL_ROUNDS = 24
MAX_FILE_CHARS = 200_000
MAX_COMMAND_CHARS = 40_000
MAX_HISTORY_TURNS = 12


def _child_env() -> dict[str, str]:
    """Return a child-process environment without JARVIS provider secrets."""
    env = dict(os.environ)
    for name in (
        "AI_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ELEVENLABS_API_KEY",
    ):
        env.pop(name, None)
    return env


def load_env_file(path: Path | None = None) -> None:
    """Load simple KEY=VALUE settings without replacing real environment values."""
    local = Path(__file__).with_name(".env")
    workspace = Path(__file__).resolve().parent.parent / ".env"
    env_path = path or (local if local.is_file() else workspace)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


load_env_file()


@dataclass
class ProviderConfig:
    provider: str
    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_env(
        cls,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> "ProviderConfig":
        name = (provider or os.getenv("AI_PROVIDER") or "").strip().lower()
        if not name:
            if os.getenv("GEMINI_API_KEY") and not os.getenv("OPENAI_API_KEY"):
                name = "gemini"
            else:
                name = "openai"
        if name not in {"openai", "gemini", "compatible"}:
            raise ValueError("AI_PROVIDER must be openai, gemini, or compatible")

        if name == "gemini":
            key = os.getenv("GEMINI_API_KEY") or os.getenv("AI_API_KEY") or ""
            url = base_url or os.getenv("AI_BASE_URL") or GEMINI_BASE_URL
            chosen_model = model or os.getenv("AI_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.7-flash"
            key_name = "GEMINI_API_KEY"
        elif name == "openai":
            key = os.getenv("OPENAI_API_KEY") or os.getenv("AI_API_KEY") or ""
            url = base_url or os.getenv("AI_BASE_URL") or OPENAI_BASE_URL
            chosen_model = model or os.getenv("AI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5-mini"
            key_name = "OPENAI_API_KEY"
        else:
            key = os.getenv("AI_API_KEY") or ""
            url = base_url or os.getenv("AI_BASE_URL") or ""
            chosen_model = model or os.getenv("AI_MODEL") or ""
            key_name = "AI_API_KEY"

        if not key:
            raise ValueError(f"No API key found. Set {key_name} in your environment.")
        if name == "compatible" and not (base_url or os.getenv("AI_BASE_URL")):
            raise ValueError("AI_BASE_URL is required when AI_PROVIDER=compatible")
        if name == "compatible" and not chosen_model:
            raise ValueError("AI_MODEL is required when AI_PROVIDER=compatible")
        return cls(name, key, url.rstrip("/"), chosen_model)


def fallback_config_from_env(primary: ProviderConfig) -> ProviderConfig | None:
    requested = (os.getenv("AI_FALLBACK_PROVIDER") or "").strip().lower()
    if not requested and primary.provider == "openai" and os.getenv("GEMINI_API_KEY"):
        requested = "gemini"
    if not requested or requested == primary.provider:
        return None
    return ProviderConfig.from_env(provider=requested)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files under a directory. Use recursive=false for a shallow listing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a UTF-8 text file after user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace one exact text fragment in a file after user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a directory, including parents, after user approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in a chosen directory after user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 1800},
                },
                "required": ["command"],
            },
        },
    },
]
TOOLS.extend(MARK2_TOOLS)


Approval = Callable[[str, dict[str, Any]], Awaitable[bool]]


def _resolve(path: str, cwd: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve()


def load_project_instructions(cwd: Path, installer_root: Path | None = None) -> str:
    """Load one identity file plus the installer conductor when available."""
    chunks: list[str] = []
    for name in ("AGENT.md", "AGENTS.md", "GEMINI.md", "CLAUDE.md"):
        path = cwd / name
        if path.is_file():
            chunks.append(f"# Project instructions from {name}\n\n{path.read_text(encoding='utf-8')}")
            break
    root = installer_root or Path(__file__).resolve().parent
    conductor = root / "fullstack-agent.md"
    if conductor.is_file() and (cwd == root or not chunks):
        chunks.append(f"# Fullstack installer conductor\n\n{conductor.read_text(encoding='utf-8')}")
    return "\n\n".join(chunks)


class LocalAgent:
    def __init__(
        self,
        config: ProviderConfig,
        cwd: Path,
        instructions: str,
        approve: Approval | None = None,
        auto_approve: bool = False,
        fallback_config: ProviderConfig | None = None,
    ) -> None:
        self.config = config
        self.fallback_config = (
            fallback_config if fallback_config is not None else fallback_config_from_env(config)
        )
        self.cwd = cwd.resolve()
        self.mark2 = Mark2Runtime(self.cwd)
        self.approve = approve
        self.auto_approve = auto_approve
        self.messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a local coding agent with tools. Follow the project instructions. "
                    "Use tools to inspect and perform work instead of telling the user to do work "
                    "you can do. Never claim a tool action succeeded until its result confirms it. "
                    "For registered projects, prefer JARVIS Mark II project tools over raw shell "
                    "commands. Discover and inspect before registering, execute only registered "
                    "start/build commands, and report verification failures honestly. "
                    "For Windows desktop requests, use the structured app, window, keyboard, mouse, "
                    "media, and UI Automation tools instead of raw shell commands. Discover installed "
                    "apps, known folders, files, or open windows first when a name/path is uncertain; "
                    "read visible window text when the user asks what an app contains; prefer semantic UI controls "
                    "over coordinates, and treat a reported verification failure as a real failure. "
                    "Use send_keys only for shortcuts and named keys; always use type_text for literal words "
                    "or sentences. If a typing or click tool reports unverified content, do not describe it "
                    "as successful; inspect the current window and retry through a verified route. "
                    "When accessibility cannot describe a visible interface, use local screen observation and "
                    "visual grounding. Use find_visual_target/click_visual_target for role, position, color, shape, "
                    "off-screen scroll search, and explicit template:name targets. Learn a local target template "
                    "only when the user clearly asks JARVIS to remember that visual target. Inspect first; click "
                    "only when the match is unique, and "
                    "accept success only when the before/after visual verification confirms a state change. "
                    "Natural descriptions such as an unlabeled settings gear may use the offline local Florence "
                    "fallback only after OCR, accessibility, descriptors, templates, and enhanced OCR miss. A "
                    "Florence-grounded click must always include an explicit expect_text, expect_absent_text, "
                    "expect_window, or expected_state result; never use a model-generated box as unverified coordinate input. "
                    "Whenever the user's requested outcome names text that should appear/disappear or a window "
                    "that should open, pass that as an explicit click_visual_text/click_visual_target postcondition; do not reduce "
                    "goal verification to generic pixel change. Use wait_for_visual_text for delayed UI states. "
                    "A generic visual change or closed window proves only an observed UI response, not that the "
                    "user's intended goal succeeded; reserve goal-success language for verified postconditions. "
                    "Never guess, request, reveal, or audit passwords and other secrets. "
                    "Plan multi-step desktop tasks from the user's desired end state, then inspect, act, "
                    "and verify each step. Discover facts such as screen geometry through tools yourself. "
                    "For duplicate window titles use exact hwnd:<handle>:pid:<pid> selectors from list_windows; "
                    "choose the previously targeted window or uniquely minimized one when restoring. "
                    "Use position_window fractions for any requested layout, including quarters and thirds. "
                    "If a named UI control is missing, inspect_ui for its AutomationId, then use "
                    "find_visual_text/click_visual_text or semantic visual-target tools if appropriate. "
                    "For a target identified by visible text, use click_visual_text; for an icon or semantic "
                    "description use click_visual_target. Never copy coordinates from visual find tools into "
                    "mouse_action, because that "
                    "bypasses stale-target and after-state verification. "
                    "The UI State Graph is privacy-safe historical evidence, not current truth: consult it "
                    "for repeated-work context when useful, but always re-observe the live window before "
                    "input and never replay an action solely because an old transition succeeded. "
                    "For stateful or multi-step interfaces, use inspect_ui_state to identify focus, active tabs, "
                    "selection, toggles, modal blockers, modified state, progress, errors, and operation state. "
                    "Express the intended after-state with expected_state on a visual click or verify_ui_state "
                    "after another action. A state contract that was already true before an action is not proof "
                    "that the action succeeded unless fresh evidence also shows the relevant transition. "
                    "Operate as a closed loop: interpret, perceive, model, plan, guard, act once, verify, recover "
                    "boundedly, and remember only privacy-safe evidence. Keep facts, inferences, and unknowns distinct. "
                    "Do not ask whether to try an available alternative for an already requested action. "
                    "Reobserve before retrying input "
                    "that may have been delivered. Never repeat a partially completed side effect blindly. "
                    "If a save dialog blocks closure, inspect it and apply the user's stated save/discard choice; "
                    "ask only if that choice is missing. A failed recognition is not authorization to guess. "
                    "Respect the user's latest requested response language throughout the conversation. "
                    "Ask only one setup question at a time. "
                    f"The selected runtime provider is {config.provider}, the model is {config.model}, "
                    f"and the non-secret API base URL is {config.base_url}; do not ask for these again.\n\n"
                    + instructions
                ),
            }
        ]
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    async def _request_with(self, config: ProviderConfig) -> dict[str, Any]:
        payload = {
            "model": config.model,
            "messages": self.messages,
            "tools": TOOLS,
            "tool_choice": "auto",
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{config.base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def send() -> dict[str, Any]:
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"API request failed ({exc.code}): {detail[:2000]}") from exc
            except TimeoutError as exc:
                raise RuntimeError("API request timed out") from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError("API returned an invalid JSON response") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Could not reach the API: {exc.reason}") from exc

        result = await asyncio.to_thread(send)
        if not isinstance(result, dict):
            raise RuntimeError("API returned a non-object response")
        if result.get("error"):
            raise RuntimeError(f"API error: {result['error']}")
        usage = result.get("usage") or {}
        self.usage["input_tokens"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.usage["output_tokens"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        choices = result.get("choices") or []
        if not choices:
            raise RuntimeError("The API returned no choices")
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise RuntimeError("API returned invalid choices")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not (message.get("content") or message.get("tool_calls")):
            raise RuntimeError("API returned an empty or invalid message")
        return message

    async def _request(self) -> dict[str, Any]:
        async def request_with_transient_retries(config: ProviderConfig) -> dict[str, Any]:
            last_error: RuntimeError | None = None
            for attempt in range(3):
                try:
                    return await self._request_with(config)
                except RuntimeError as exc:
                    last_error = exc
                    detail = str(exc)
                    transient = any(
                        marker in detail
                        for marker in ("(429)", "(500)", "(502)", "(503)", "(504)")
                    )
                    if not transient or attempt == 2:
                        raise
                    await asyncio.sleep(1.0 * (2 ** attempt))
            raise last_error or RuntimeError("API request failed")

        try:
            return await request_with_transient_retries(self.config)
        except RuntimeError as primary_error:
            if not self.fallback_config:
                raise
            failed = self.config
            self.config = self.fallback_config
            self.fallback_config = None
            print(
                f"Primary provider {failed.provider} failed; continuing with "
                f"{self.config.provider}/{self.config.model}.",
                file=sys.stderr,
            )
            try:
                return await request_with_transient_retries(self.config)
            except RuntimeError as fallback_error:
                raise RuntimeError(
                    f"Primary provider {failed.provider} failed ({primary_error}); "
                    f"fallback provider {self.config.provider} also failed ({fallback_error})"
                ) from fallback_error

    async def _allowed(self, name: str, args: dict[str, Any]) -> bool:
        if name in {"list_files", "read_file"} | READ_ONLY_MARK2_TOOLS or self.auto_approve:
            return True
        if self.approve:
            return await self.approve(name, args)
        return False

    async def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name in MARK2_TOOL_NAMES:
                if not await self._allowed(name, args):
                    result = "denied by user"
                else:
                    result = await asyncio.to_thread(self.mark2.execute, name, args)
                self.mark2.audit(name, args, result)
                return result

            if name == "list_files":
                root = _resolve(str(args["path"]), self.cwd)
                if not root.is_dir():
                    return f"error: directory does not exist: {root}"
                recursive = bool(args.get("recursive", False))
                entries = root.rglob("*") if recursive else root.iterdir()
                items = []
                for entry in entries:
                    items.append(str(entry.relative_to(root)).replace("\\", "/") + ("/" if entry.is_dir() else ""))
                    if len(items) >= 500:
                        items.append("... listing truncated at 500 entries")
                        break
                return "\n".join(items) or "(empty directory)"

            if name == "read_file":
                path = _resolve(str(args["path"]), self.cwd)
                text = path.read_text(encoding="utf-8")
                if len(text) > MAX_FILE_CHARS:
                    return text[:MAX_FILE_CHARS] + "\n... file truncated"
                return text

            if not await self._allowed(name, args):
                return "denied by user"

            if name == "write_file":
                path = _resolve(str(args["path"]), self.cwd)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(args["content"]), encoding="utf-8")
                return f"wrote {path}"

            if name == "replace_in_file":
                path = _resolve(str(args["path"]), self.cwd)
                text = path.read_text(encoding="utf-8")
                old = str(args["old"])
                count = text.count(old)
                if count != 1:
                    return f"error: expected exactly one match, found {count}"
                path.write_text(text.replace(old, str(args["new"]), 1), encoding="utf-8")
                return f"updated {path}"

            if name == "create_directory":
                path = _resolve(str(args["path"]), self.cwd)
                path.mkdir(parents=True, exist_ok=True)
                return f"created {path}"

            if name == "run_command":
                command = str(args["command"])
                run_cwd = _resolve(str(args.get("cwd") or self.cwd), self.cwd)
                timeout = min(max(int(args.get("timeout") or 300), 1), 1800)

                def run() -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        command,
                        cwd=run_cwd,
                        shell=True,
                        env=_child_env(),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                        encoding="utf-8",
                        errors="replace",
                    )

                completed = await asyncio.to_thread(run)
                output = completed.stdout or ""
                if len(output) > MAX_COMMAND_CHARS:
                    output = output[-MAX_COMMAND_CHARS:] + "\n... output truncated to final characters"
                return f"exit code: {completed.returncode}\n{output}"

            return f"error: unknown tool {name}"
        except subprocess.TimeoutExpired:
            result = "error: command timed out"
            if name in MARK2_TOOL_NAMES:
                self.mark2.audit(name, args, result)
            return result
        except Exception as exc:
            result = f"error: {type(exc).__name__}: {exc}"
            if name in MARK2_TOOL_NAMES:
                self.mark2.audit(name, args, result)
            return result

    async def ask(self, prompt: str) -> str:
        self.compact(MAX_HISTORY_TURNS)
        checkpoint = list(self.messages)
        self.messages.append({"role": "user", "content": prompt})
        recovery_pending = False
        recovery_rounds = 0
        failure_counts: dict[str, int] = {}
        visual_observed = False
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                message = await self._request()
                clean: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.get("content"),
                }
                if message.get("tool_calls"):
                    clean["tool_calls"] = message["tool_calls"]
                self.messages.append(clean)
                calls = message.get("tool_calls") or []
                if not calls:
                    if recovery_pending and recovery_rounds < 2:
                        self.messages.pop()  # Do not speak a premature hand-off.
                        self.messages.append({"role": "system", "content":
                            "The requested desktop action has an unresolved tool failure. "
                            "Inspect current state and try a different applicable tool within the original "
                            "request before handing the task back. Do not repeat uncertain input or denied "
                            "actions. If inspection establishes a missing user choice or unavailable "
                            "capability, explain the specific blocker. Recovery budget is bounded."})
                        recovery_pending = False
                        recovery_rounds += 1
                        continue
                    return str(message.get("content") or "")
                for call in calls:
                    function = call.get("function") or {}
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("tool arguments must be a JSON object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        result = f"error: invalid tool arguments: {exc}"
                    else:
                        name = str(function.get("name") or "")
                        signature = name + json.dumps(args, sort_keys=True)
                        if (visual_observed and name == 'mouse_action'
                                and args.get('action') in {'click', 'double_click'}):
                            result = ('error: raw click after visual observation blocked; no input delivered. '
                                      'Use click_visual_text or click_visual_target so the target is reacquired, '
                                      'stabilized, and verified immediately before input.')
                        elif failure_counts.get(signature, 0) >= 2:
                            result = "error: repeated failed action blocked; inspect state or use a different approach"
                        else:
                            result = await self._run_tool(name, args)
                        if result.lower().startswith('error:'):
                            failure_counts[signature] = failure_counts.get(signature, 0) + 1
                        elif name in {'observe_screen', 'find_visual_text', 'find_visual_target', 'wait_for_visual_text'}:
                            visual_observed = True
                        if name in {'interact_ui', 'control_window', 'click_visual_text', 'click_visual_target', 'learn_visual_target', 'wait_for_visual_text', 'position_window',
                                    'type_text', 'send_keys', 'launch_app', 'mouse_action'}:
                            recovery_pending = result.lower().startswith('error:')
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": result,
                        }
                    )
            raise RuntimeError(f"Model exceeded the {MAX_TOOL_ROUNDS}-round tool limit")
        except BaseException:
            # A cancelled or failed request must not poison the next turn with
            # an unmatched user/tool message. Tool side effects remain in the
            # audit log and are re-verified before any later action.
            self.messages = checkpoint
            raise

    def compact(self, max_turns: int = 6) -> None:
        """Keep complete recent user turns; never start history on a tool message."""
        max_turns = max(1, int(max_turns))
        user_starts = [
            index for index, message in enumerate(self.messages)
            if message.get("role") == "user"
        ]
        if len(user_starts) <= max_turns:
            return
        start = user_starts[-max_turns]
        self.messages = self.messages[:1] + self.messages[start:]

    def clear(self) -> None:
        self.messages = self.messages[:1]


async def terminal_approval(name: str, args: dict[str, Any]) -> bool:
    if name == "run_command":
        detail = f"run `{args.get('command', '')}` in {args.get('cwd') or Path.cwd()}"
    else:
        detail = f"{name} on {args.get('path', '')}"
    answer = await asyncio.to_thread(input, f"\nPermission required: {detail}\nAllow? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run fullstack-agent with OpenAI, Gemini, or another OpenAI-compatible API")
    parser.add_argument("prompt", nargs="?", help="first message; the conversation remains interactive unless --once is used")
    parser.add_argument("--provider", choices=["openai", "gemini", "compatible"])
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--yes", action="store_true", help="auto-approve file writes and shell commands")
    parser.add_argument("--once", action="store_true", help="exit after the first response")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    return parser


async def _main(args: argparse.Namespace) -> int:
    cwd = args.cwd.expanduser().resolve()
    try:
        config = ProviderConfig.from_env(args.provider, args.model, args.base_url)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    instructions = load_project_instructions(cwd)
    agent = LocalAgent(config, cwd, instructions, terminal_approval, args.yes)
    print(f"fullstack-agent using {config.provider}/{config.model}. Type /exit to close.")

    pending = args.prompt
    while True:
        if pending is None:
            try:
                pending = await asyncio.to_thread(input, "\nYou: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
        command = pending.strip()
        if command in {"/exit", "/quit"}:
            break
        if command == "/clear":
            agent.clear()
            print("Conversation cleared.")
        elif command:
            try:
                reply = await agent.ask(command)
                print(f"\nAssistant: {reply}")
            except (RuntimeError, KeyboardInterrupt) as exc:
                print(f"\nError: {exc}", file=sys.stderr)
                if args.once:
                    return 1
        if args.once:
            break
        pending = None
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_build_parser().parse_args())))
