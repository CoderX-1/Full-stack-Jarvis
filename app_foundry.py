"""Validated, versioned storage and launch gates for AI-authored desktop apps."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


_SAFE_IMPORT_ROOTS = {
    "calendar", "collections", "colorsys", "dataclasses", "datetime",
    "decimal", "enum", "fractions", "functools", "hashlib", "heapq",
    "itertools", "json", "math", "operator", "random", "re", "statistics",
    "string", "textwrap", "time", "tkinter", "typing", "uuid",
}
_BLOCKED_CALLS = {
    "breakpoint", "compile", "delattr", "eval", "exec", "exit", "getattr",
    "globals", "help", "input", "locals", "open", "quit", "setattr", "vars",
    "__import__",
}
_BLOCKED_ATTRIBUTES = {
    "__bases__", "__builtins__", "__class__", "__code__", "__dict__",
    "__func__", "__globals__", "__mro__", "__subclasses__", "call", "eval",
    "evalfile", "loadtk",
}
_ALLOWED_SUFFIXES = {".py", ".json", ".md", ".txt"}
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class AppFoundryError(ValueError):
    """A generated bundle failed a deterministic foundry gate."""


class _PythonPolicy(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root not in _SAFE_IMPORT_ROOTS:
                self.errors.append(f"line {node.lineno}: import {root!r} is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        root = (node.module or "").split(".", 1)[0]
        if node.level or root not in _SAFE_IMPORT_ROOTS:
            self.errors.append(f"line {node.lineno}: import from {node.module!r} is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
            self.errors.append(f"line {node.lineno}: call to {node.func.id!r} is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr in _BLOCKED_ATTRIBUTES or node.attr.startswith("__"):
            self.errors.append(f"line {node.lineno}: attribute {node.attr!r} is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id.startswith("__"):
            self.errors.append(f"line {node.lineno}: dunder name {node.id!r} is not allowed")
        self.generic_visit(node)


class AppFoundry:
    """Create immutable app versions that pass path, syntax, and policy gates."""

    def __init__(self, state_dir: Path) -> None:
        self.root = (state_dir / "generated_apps").resolve()
        self._live: dict[str, subprocess.Popen[bytes]] = {}

    @staticmethod
    def _slug(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        if not 2 <= len(slug) <= 60:
            raise AppFoundryError("App name must contain 2-60 letters or numbers")
        return slug

    @staticmethod
    def _relative_file_path(value: str) -> PurePosixPath:
        normalized = value.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(part in {"", "."} for part in path.parts)
            or path.suffix.casefold() not in _ALLOWED_SUFFIXES
        ):
            raise AppFoundryError(f"Unsafe or unsupported bundle path: {value!r}")
        if len(path.parts) > 4:
            raise AppFoundryError(f"Bundle path is too deeply nested: {value!r}")
        return path

    @staticmethod
    def _validate_python(path: PurePosixPath, content: str) -> None:
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            raise AppFoundryError(
                f"{path}: syntax error at line {exc.lineno}: {exc.msg}"
            ) from exc
        policy = _PythonPolicy()
        policy.visit(tree)
        if policy.errors:
            raise AppFoundryError(f"{path}: policy rejected: " + "; ".join(policy.errors[:8]))
        compile(tree, str(path), "exec")

    @staticmethod
    def _clean_env() -> dict[str, str]:
        clean: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if not any(marker in upper for marker in _SECRET_MARKERS):
                clean[key] = value
        clean["PYTHONNOUSERSITE"] = "1"
        clean["PYTHONDONTWRITEBYTECODE"] = "1"
        return clean

    def create(
        self,
        name: str,
        description: str,
        entrypoint: str,
        files: list[dict[str, Any]],
    ) -> str:
        slug = self._slug(name)
        if not isinstance(files, list) or not 1 <= len(files) <= 12:
            raise AppFoundryError("Provide between 1 and 12 bundle files")
        if len(description) > 1000:
            raise AppFoundryError("Description exceeds 1000 characters")

        normalized: dict[PurePosixPath, str] = {}
        total_bytes = 0
        for item in files:
            if not isinstance(item, dict):
                raise AppFoundryError("Each bundle file must be an object")
            path = self._relative_file_path(str(item.get("path") or ""))
            content = item.get("content")
            if not isinstance(content, str):
                raise AppFoundryError(f"{path}: content must be text")
            size = len(content.encode("utf-8"))
            if size > 200_000:
                raise AppFoundryError(f"{path}: file exceeds 200 KB")
            total_bytes += size
            if total_bytes > 750_000:
                raise AppFoundryError("Bundle exceeds 750 KB")
            if path in normalized:
                raise AppFoundryError(f"Duplicate bundle path: {path}")
            if path.suffix.casefold() == ".py":
                self._validate_python(path, content)
            elif path.suffix.casefold() == ".json":
                try:
                    json.loads(content)
                except json.JSONDecodeError as exc:
                    raise AppFoundryError(f"{path}: invalid JSON: {exc.msg}") from exc
            normalized[path] = content

        entry = self._relative_file_path(entrypoint)
        if entry.suffix.casefold() != ".py" or entry not in normalized:
            raise AppFoundryError("Entrypoint must name a Python file included in the bundle")

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        app_root = self.root / slug
        version_dir = app_root / "versions" / stamp
        version_dir.mkdir(parents=True, exist_ok=False)
        hashes: dict[str, str] = {}
        for relative, content in normalized.items():
            target = version_dir.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            hashes[str(relative)] = hashlib.sha256(content.encode("utf-8")).hexdigest()

        manifest = {
            "schema_version": 1,
            "name": name.strip(),
            "slug": slug,
            "description": description.strip(),
            "entrypoint": str(entry),
            "version": stamp,
            "created_at": now.isoformat(),
            "status": "verified",
            "gates": ["path-confinement", "size-bounds", "ast-policy", "syntax-compile"],
            "files": hashes,
        }
        (version_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        app_root.mkdir(parents=True, exist_ok=True)
        pointer = app_root / "CURRENT"
        temporary = app_root / "CURRENT.tmp"
        temporary.write_text(stamp + "\n", encoding="utf-8")
        temporary.replace(pointer)
        return (
            f"Verified app created: {name.strip()} ({slug}) version={stamp}; "
            f"files={len(normalized)}; bytes={total_bytes}; entrypoint={entry}; "
            "not launched"
        )

    def _current(self, name: str) -> tuple[str, Path, dict[str, Any]]:
        slug = self._slug(name)
        app_root = self.root / slug
        pointer = app_root / "CURRENT"
        if not pointer.is_file():
            raise AppFoundryError(f"No generated app named {name!r}")
        version = pointer.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"\d{8}T\d{12}Z", version):
            raise AppFoundryError("Current app version pointer is invalid")
        version_dir = (app_root / "versions" / version).resolve()
        version_dir.relative_to(self.root)
        manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
        return slug, version_dir, manifest

    def list_apps(self) -> str:
        if not self.root.is_dir():
            return "No generated apps yet."
        records: list[str] = []
        for app_root in sorted(path for path in self.root.iterdir() if path.is_dir()):
            try:
                _, _, manifest = self._current(app_root.name)
            except (AppFoundryError, OSError, json.JSONDecodeError):
                continue
            records.append(
                f"{manifest['name']} | id={manifest['slug']} | "
                f"version={manifest['version']} | status={manifest['status']} | "
                f"entrypoint={manifest['entrypoint']}"
            )
        return "\n".join(records) if records else "No generated apps yet."

    def launch(self, name: str) -> str:
        slug, version_dir, manifest = self._current(name)
        existing = self._live.get(slug)
        if existing and existing.poll() is None:
            return f"Verified app {manifest['name']} is already running; pid={existing.pid}"
        if manifest.get("status") != "verified":
            raise AppFoundryError("Only verified app versions can launch")
        entrypoint = self._relative_file_path(str(manifest.get("entrypoint") or ""))
        target = version_dir.joinpath(*entrypoint.parts).resolve()
        target.relative_to(version_dir)
        expected = manifest.get("files", {}).get(str(entrypoint))
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if not expected or actual != expected:
            raise AppFoundryError("Entrypoint integrity check failed; launch refused")
        process = subprocess.Popen(
            [sys.executable, "-I", str(target)],
            cwd=version_dir,
            env=self._clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        time.sleep(0.35)
        if process.poll() is not None:
            return f"error: verified app exited during launch; exit_code={process.returncode}"
        self._live[slug] = process
        return (
            f"Verified app launched: {manifest['name']}; pid={process.pid}; "
            f"version={manifest['version']}; integrity=verified"
        )
