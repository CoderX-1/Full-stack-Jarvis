"""Project-control foundation for JARVIS Mark II."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKIP_DIRS = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "node_modules",
}

READ_ONLY_MARK2_TOOLS = {
    "list_projects",
    "discover_projects",
    "project_status",
    "git_status",
    "search_project_files",
    "check_local_url",
    "recent_audit",
}

MARK2_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List projects registered with JARVIS.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_projects",
            "description": "Find likely project folders directly inside the agent home without registering or changing them.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_project",
            "description": "Register an exact project path and its approved commands. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "start_command": {"type": "string"},
                    "build_command": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": "string"},
                    "open_command": {"type": "string"},
                },
                "required": ["name", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_status",
            "description": "Report registered metadata, current-session process state, localhost health, and git status.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Read git branch and working-tree status for a registered project.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_project_files",
            "description": "Safely search filenames and UTF-8 text inside a registered project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["name", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_local_url",
            "description": "Check an HTTP endpoint restricted to localhost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_project",
            "description": "Open a registered project in its configured app or the system file browser. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_project",
            "description": "Start only the registered dev command and verify its process or localhost URL. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verify_timeout": {"type": "integer", "minimum": 1, "maximum": 60},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_project",
            "description": "Stop only a project process started by this JARVIS session. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_project_build",
            "description": "Run only the registered build command and verify its exit code. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 1800},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_audit",
            "description": "Read recent JARVIS Mark II tool audit records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
    },
]
MARK2_TOOL_NAMES = {item["function"]["name"] for item in MARK2_TOOLS}


class Mark2Runtime:
    """Persistent project registry, verified actions, and append-only audit."""

    def __init__(self, agent_home: Path) -> None:
        self.agent_home = agent_home.resolve()
        self.state_dir = self.agent_home / ".jarvis"
        self.registry_path = self.state_dir / "projects.json"
        self.process_path = self.state_dir / "processes.json"
        self.audit_path = self.state_dir / "audit.jsonl"
        self._live_processes: dict[str, subprocess.Popen[str]] = {}
        self._process_uses_shell: dict[str, bool] = {}

    def _ensure_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _save_json(self, path: Path, value: Any) -> None:
        self._ensure_state()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _safe_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.agent_home / path
        path = path.resolve()
        try:
            path.relative_to(self.agent_home)
        except ValueError as exc:
            raise ValueError("Project paths must stay inside the agent home folder") from exc
        return path

    def _projects(self) -> dict[str, dict[str, Any]]:
        data = self._load_json(self.registry_path, {"projects": {}})
        projects = data.get("projects") if isinstance(data, dict) else {}
        return projects if isinstance(projects, dict) else {}

    def audit(self, tool: str, args: dict[str, Any], result: str) -> None:
        self._ensure_state()
        safe_args = self._sanitize_for_audit(args)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": safe_args,
            "success": not result.lower().startswith(("error:", "denied")),
            "result": result[:1000],
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    @classmethod
    def _sanitize_for_audit(cls, value: Any) -> Any:
        if isinstance(value, dict):
            safe = {}
            for key, item in value.items():
                normalized = str(key).casefold()
                if normalized in {"content", "new", "old"} or any(
                    marker in normalized
                    for marker in ("api_key", "apikey", "password", "secret", "token")
                ):
                    safe[str(key)] = "[REDACTED]"
                else:
                    safe[str(key)] = cls._sanitize_for_audit(item)
            return safe
        if isinstance(value, list):
            return [cls._sanitize_for_audit(item) for item in value]
        return value

    def list_projects(self) -> str:
        projects = self._projects()
        if not projects:
            return "No projects registered yet. Use discover_projects, then register_project."
        rows = []
        for name, item in sorted(projects.items()):
            rows.append(
                f"{name}: {item['path']}"
                + (f" | {item.get('description')}" if item.get("description") else "")
            )
        return "\n".join(rows)

    def _get_project(self, name: str) -> dict[str, Any]:
        projects = self._projects()
        exact = projects.get(name)
        if exact:
            return exact
        matches = [value for key, value in projects.items() if key.lower() == name.lower()]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"Unknown project: {name}. Use list_projects first.")

    def discover_projects(self) -> str:
        candidates = []
        for folder in sorted(self.agent_home.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            markers = [
                marker
                for marker in ("package.json", "pyproject.toml", "requirements.txt", ".git")
                if (folder / marker).exists()
            ]
            if markers:
                candidates.append(f"{folder.name}: {folder} | markers={','.join(markers)}")
        return "\n".join(candidates) or "No project candidates found."

    def register_project(
        self,
        name: str,
        path: str,
        start_command: str = "",
        build_command: str = "",
        url: str = "",
        description: str = "",
        open_command: str = "",
    ) -> str:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 80:
            raise ValueError("Project name must be between 1 and 80 characters")
        project_path = self._safe_path(path)
        if not project_path.is_dir():
            raise ValueError(f"Project directory does not exist: {project_path}")
        if url:
            self._validate_local_url(url)
        projects = self._projects()
        projects[clean_name] = {
            "name": clean_name,
            "path": str(project_path).replace("\\", "/"),
            "start_command": start_command.strip(),
            "build_command": build_command.strip(),
            "url": url.strip(),
            "description": description.strip(),
            "open_command": open_command.strip(),
        }
        self._save_json(self.registry_path, {"version": 1, "projects": projects})
        return f"Registered project {clean_name} at {project_path}"

    def git_status(self, name: str) -> str:
        project = self._get_project(name)
        path = self._safe_path(project["path"])
        completed = subprocess.run(
            ["git", "-C", str(path), "status", "--short", "--branch"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
        output = (completed.stdout or "").strip()
        if completed.returncode:
            return f"error: git status failed ({completed.returncode}): {output}"
        return output or "Working tree clean."

    @staticmethod
    def _validate_local_url(url: str) -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http and https URLs are supported")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Health checks are restricted to localhost")
        return parsed

    def check_local_url(self, url: str, timeout: int = 5) -> str:
        self._validate_local_url(url)
        try:
            with urllib.request.urlopen(url, timeout=max(1, min(timeout, 30))) as response:
                status = int(response.status)
                return f"HTTP {status} from {url}"
        except urllib.error.HTTPError as exc:
            return f"error: HTTP {exc.code} from {url}"
        except (urllib.error.URLError, TimeoutError) as exc:
            return f"error: could not reach {url}: {getattr(exc, 'reason', exc)}"

    @staticmethod
    def _child_env() -> dict[str, str]:
        env = dict(os.environ)
        for name in (
            "AI_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "ELEVENLABS_API_KEY",
        ):
            env.pop(name, None)
        return env

    @staticmethod
    def _launch_args(command: str) -> tuple[str | list[str], bool]:
        if os.name != "nt":
            return command, True
        parts = shlex.split(command, posix=False)
        parts = [
            part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
            for part in parts
        ]
        executable = shutil.which(parts[0]) if parts else None
        if executable and Path(executable).suffix.casefold() not in {".bat", ".cmd"}:
            parts[0] = executable
            return parts, False
        return command, True

    def open_project(self, name: str) -> str:
        project = self._get_project(name)
        path = self._safe_path(project["path"])
        command = project.get("open_command") or ""
        if command:
            subprocess.Popen(
                command,
                cwd=path,
                shell=True,
                env=self._child_env(),
            )
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return f"Opened project {name} at {path}"

    def start_project(self, name: str, verify_timeout: int = 20) -> str:
        project = self._get_project(name)
        command = project.get("start_command") or ""
        if not command:
            return f"error: project {name} has no registered start command"
        existing = self._live_processes.get(project["name"])
        if existing and existing.poll() is None:
            return f"Project {name} is already running with PID {existing.pid}"
        path = self._safe_path(project["path"])
        self._ensure_state()
        log_path = self.state_dir / f"{self._slug(project['name'])}-server.log"
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        launch_args, uses_shell = self._launch_args(command)
        with log_path.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(
                launch_args,
                cwd=path,
                shell=uses_shell,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                env=self._child_env(),
                creationflags=creationflags,
            )
        self._live_processes[project["name"]] = process
        self._process_uses_shell[project["name"]] = uses_shell
        self._save_json(
            self.process_path,
            {
                "processes": {
                    project["name"]: {
                        "pid": process.pid,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "log": str(log_path).replace("\\", "/"),
                    }
                }
            },
        )
        url = project.get("url") or ""
        deadline = time.monotonic() + max(1, min(verify_timeout, 60))
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return f"error: project {name} exited with code {process.returncode}; log={log_path}"
            health = self.check_local_url(url) if url else ""
            if not url or not health.startswith("error:"):
                return f"Started and verified project {name}; PID {process.pid}" + (
                    f"; {health}" if url else ""
                )
            time.sleep(0.5)
        return f"error: project {name} started with PID {process.pid}, but {url} did not become ready"

    def stop_project(self, name: str) -> str:
        project = self._get_project(name)
        process = self._live_processes.get(project["name"])
        if not process or process.poll() is not None:
            return "error: no process started by this JARVIS session is running for that project"
        uses_shell = self._process_uses_shell.get(project["name"], False)
        if os.name == "nt" and uses_shell:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            if completed.returncode:
                return f"error: could not stop PID {process.pid}: {(completed.stdout or '').strip()}"
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return f"error: stop command ran, but PID {process.pid} is still active"
        self._live_processes.pop(project["name"], None)
        self._process_uses_shell.pop(project["name"], None)
        return f"Stopped project {name}; verified PID {process.pid} is no longer active"

    def project_status(self, name: str) -> str:
        project = self._get_project(name)
        process = self._live_processes.get(project["name"])
        running = bool(process and process.poll() is None)
        details = [
            f"name={project['name']}",
            f"path={project['path']}",
            f"started_by_this_session={running}",
        ]
        if running and process:
            details.append(f"pid={process.pid}")
        url = project.get("url") or ""
        if url:
            details.append(f"health={self.check_local_url(url)}")
        details.append(f"git={self.git_status(project['name'])}")
        return "\n".join(details)

    def search_project_files(
        self,
        name: str,
        query: str,
        max_results: int = 50,
    ) -> str:
        project = self._get_project(name)
        root = self._safe_path(project["path"])
        needle = query.casefold().strip()
        if not needle:
            raise ValueError("Search query cannot be empty")
        limit = max(1, min(max_results, 200))
        matches: list[str] = []
        for path in root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
                continue
            if path.is_symlink():
                continue
            try:
                path.resolve().relative_to(root)
            except ValueError:
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            if needle in relative.casefold():
                matches.append(relative)
            elif path.stat().st_size <= 2_000_000:
                try:
                    for line_number, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(),
                        start=1,
                    ):
                        if needle in line.casefold():
                            matches.append(f"{relative}:{line_number}: {line.strip()[:240]}")
                            break
                except (OSError, UnicodeDecodeError):
                    pass
            if len(matches) >= limit:
                break
        return "\n".join(matches) if matches else f"No matches for {query!r} in {name}."

    def run_project_build(self, name: str, timeout: int = 600) -> str:
        project = self._get_project(name)
        command = project.get("build_command") or ""
        if not command:
            return f"error: project {name} has no registered build command"
        path = self._safe_path(project["path"])
        completed = subprocess.run(
            command,
            cwd=path,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(1, min(timeout, 1800)),
            encoding="utf-8",
            errors="replace",
            env=self._child_env(),
        )
        output = (completed.stdout or "")[-12_000:]
        if completed.returncode:
            return f"error: build failed with exit code {completed.returncode}\n{output}"
        return f"Build verified successfully with exit code 0\n{output}"

    def recent_audit(self, limit: int = 20) -> str:
        if not self.audit_path.is_file():
            return "No audited actions yet."
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-max(1, min(limit, 100)):])

    def execute(self, name: str, args: dict[str, Any]) -> str:
        routes = {
            "list_projects": lambda: self.list_projects(),
            "discover_projects": lambda: self.discover_projects(),
            "register_project": lambda: self.register_project(
                str(args["name"]),
                str(args["path"]),
                str(args.get("start_command") or ""),
                str(args.get("build_command") or ""),
                str(args.get("url") or ""),
                str(args.get("description") or ""),
                str(args.get("open_command") or ""),
            ),
            "project_status": lambda: self.project_status(str(args["name"])),
            "git_status": lambda: self.git_status(str(args["name"])),
            "search_project_files": lambda: self.search_project_files(
                str(args["name"]),
                str(args["query"]),
                int(args.get("max_results") or 50),
            ),
            "check_local_url": lambda: self.check_local_url(
                str(args["url"]),
                int(args.get("timeout") or 5),
            ),
            "open_project": lambda: self.open_project(str(args["name"])),
            "start_project": lambda: self.start_project(
                str(args["name"]),
                int(args.get("verify_timeout") or 20),
            ),
            "stop_project": lambda: self.stop_project(str(args["name"])),
            "run_project_build": lambda: self.run_project_build(
                str(args["name"]),
                int(args.get("timeout") or 600),
            ),
            "recent_audit": lambda: self.recent_audit(int(args.get("limit") or 20)),
        }
        if name not in routes:
            return f"error: unknown Mark II tool {name}"
        return routes[name]()

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "project"
