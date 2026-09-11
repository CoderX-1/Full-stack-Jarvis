"""OpenAI-compatible brain for Backtalk.

This file is copied over Backtalk's Claude SDK brain by
``patch_backtalk.py``.  It deliberately preserves Backtalk's ``WarmBrain``
interface while routing model calls through fullstack-agent/agent.py.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backtalk.config import CFG, DISCIPLINE
from backtalk.vlog import log


@dataclass
class PermissionResultAllow:
    behavior: str = "allow"


@dataclass
class PermissionResultDeny:
    behavior: str = "deny"
    message: str = "Denied by the user."
    interrupt: bool = False


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
SESSION_FILE = os.path.join(CFG["signals_dir"], ".backtalk_session")


def _load_runner():
    agent_root = Path(CFG["agent_dir"]).expanduser()
    candidates = [
        agent_root / "core" / "agent.py",
        agent_root / "fullstack-agent-main" / "agent.py",
        agent_root / "fullstack-agent" / "agent.py",
        Path(__file__).resolve().parents[2] / "fullstack-agent-main" / "agent.py",
        Path(__file__).resolve().parents[2] / "fullstack-agent" / "agent.py",
        Path(__file__).resolve().parents[2] / "agent.py",
    ]
    for path in candidates:
        if path.is_file():
            runner_dir = str(path.parent)
            if runner_dir not in sys.path:
                sys.path.insert(0, runner_dir)
            spec = importlib.util.spec_from_file_location("fullstack_agent_runner", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                return module
    raise RuntimeError(
        "Could not find core/agent.py or fullstack-agent(-main)/agent.py."
    )


class WarmBrain:
    def __init__(self, model: str | None = None, can_use_tool=None, resume_id: str | None = None):
        self.provider = str(CFG.get("provider") or os.getenv("AI_PROVIDER") or "openai").lower()
        provider_default = "gemini-3.7-flash" if self.provider == "gemini" else "gpt-5-mini"
        self.model = model or CFG.get("model") or os.getenv("AI_MODEL") or provider_default
        self._base_url = CFG.get("base_url") or os.getenv("AI_BASE_URL") or None
        self._can_use_tool = can_use_tool
        self._runner = None
        self._agent = None
        self._interrupted = False
        self.session = {"turns": 0, "out_tokens": 0, "in_tokens": 0, "cost": 0.0}

    async def _approve(self, name: str, args: dict[str, Any]) -> bool:
        if not self._can_use_tool:
            return False
        mapping = {
            "write_file": "Write",
            "replace_in_file": "Edit",
            "create_directory": "Write",
            "run_command": "Bash",
        }
        tool = mapping.get(name, name)
        tool_input = dict(args)
        if "path" in tool_input:
            tool_input["file_path"] = tool_input["path"]
        if "command" in tool_input:
            tool_input["command"] = tool_input["command"]
        ctx = SimpleNamespace(display_name=tool, description=f"Backtalk {name} action")
        decision = await self._can_use_tool(tool, tool_input, ctx)
        return getattr(decision, "behavior", "deny") == "allow"

    async def start(self):
        self._runner = _load_runner()
        try:
            config = self._runner.ProviderConfig.from_env(
                provider=self.provider,
                model=self.model,
                base_url=self._base_url,
            )
        except ValueError as exc:
            # Request-level failover cannot help if the configured primary
            # has no key at startup.  In that case boot directly on the
            # configured fallback, using its own model and endpoint rather
            # than carrying OpenAI's values across to Gemini.
            fallback = (os.getenv("AI_FALLBACK_PROVIDER") or "").strip().lower()
            if ("No API key found" not in str(exc) or not fallback
                    or fallback == self.provider):
                raise
            log(f"[brain] {self.provider} unavailable at startup; "
                f"using {fallback}")
            config = self._runner.ProviderConfig.from_env(provider=fallback)
        instructions = self._runner.load_project_instructions(Path(CFG["agent_dir"]).expanduser())
        instructions = instructions + "\n\n" + DISCIPLINE
        auto = CFG.get("permission_mode") == "bypassPermissions"
        self._agent = self._runner.LocalAgent(
            config,
            Path(CFG["agent_dir"]).expanduser(),
            instructions,
            approve=self._approve,
            auto_approve=auto,
        )
        self.model = config.model
        self.provider = config.provider
        log(f"[brain] provider={config.provider} endpoint={config.base_url}")

    async def set_permission_mode(self, backtalk_mode: str):
        if self._agent:
            self._agent.auto_approve = backtalk_mode == "bypassPermissions"

    async def context_usage(self):
        return None

    async def command(self, cmd: str) -> str:
        if not self._agent:
            return "error: brain is not connected"
        if cmd == "/clear":
            self._agent.clear()
            return "Conversation cleared."
        if cmd == "/compact":
            self._agent.compact(6)
            return "Conversation compacted."
        if cmd.startswith("/model "):
            self.model = cmd.split(" ", 1)[1].strip()
            self._agent.config.model = self.model
            return f"Model changed to {self.model}."
        if cmd.startswith("/effort "):
            return "Reasoning effort is controlled by the selected compatible endpoint."
        return "error: unsupported command"

    async def interrupt(self):
        self._interrupted = True

    async def reset_turn(self, timeout: float = 8.0):
        self._interrupted = False

    async def stop(self):
        self._agent = None

    async def ask_stream(self, utterance: str):
        if not self._agent:
            raise RuntimeError("brain is not connected")
        self._interrupted = False
        reply = await self._agent.ask(utterance)
        self.session["turns"] += 1
        self.session["in_tokens"] = self._agent.usage["input_tokens"]
        self.session["out_tokens"] = self._agent.usage["output_tokens"]
        for sentence in _SENTENCE_END.split(reply.strip()):
            if self._interrupted:
                break
            if sentence:
                yield sentence


if __name__ == "__main__":
    import asyncio

    async def demo():
        brain = WarmBrain()
        await brain.start()
        async for part in brain.ask_stream("Reply with one short sentence."):
            print(part, flush=True)

    asyncio.run(demo())
