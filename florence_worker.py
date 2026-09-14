"""Offline localhost Florence-2 phrase-grounding worker for JARVIS."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import io
import json
import os
import sys
import threading
import time
import types
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


for secret_name in (
    "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ELEVENLABS_API_KEY",
):
    os.environ.pop(secret_name, None)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
warnings.filterwarnings("ignore", category=SyntaxWarning)


def _load_module(package: str, name: str, path: Path):
    qualified = f"{package}.{name}"
    spec = importlib.util.spec_from_file_location(qualified, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load local Florence module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


class FlorenceGrounder:
    def __init__(self, model_dir: Path) -> None:
        import torch
        from transformers import AutoProcessor

        self.torch = torch
        self.lock = threading.Lock()
        package = "jarvis_local_florence2"
        pkg = types.ModuleType(package)
        pkg.__path__ = [str(model_dir)]
        sys.modules[package] = pkg
        config_module = _load_module(
            package, "configuration_florence2", model_dir / "configuration_florence2.py"
        )
        model_module = _load_module(
            package, "modeling_florence2", model_dir / "modeling_florence2.py"
        )
        self.processor = AutoProcessor.from_pretrained(
            model_dir, trust_remote_code=True, local_files_only=True
        )
        config = config_module.Florence2Config.from_pretrained(
            model_dir, local_files_only=True
        )
        self.model = model_module.Florence2ForConditionalGeneration.from_pretrained(
            model_dir,
            config=config,
            local_files_only=True,
            torch_dtype=torch.float32,
        ).eval()
        self.parameters = sum(item.numel() for item in self.model.parameters())

    def ground(self, image: Any, query: str) -> tuple[list[dict[str, Any]], float]:
        task = "<CAPTION_TO_PHRASE_GROUNDING>"
        inputs = self.processor(text=task + query, images=image, return_tensors="pt")
        started = time.perf_counter()
        with self.lock, self.torch.inference_mode():
            generated = self.model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"].to(self.torch.float32),
                max_new_tokens=96,
                do_sample=False,
                num_beams=1,
                early_stopping=False,
            )
        elapsed = time.perf_counter() - started
        generated_text = self.processor.batch_decode(
            generated, skip_special_tokens=False
        )[0]
        parsed = self.processor.post_process_generation(
            generated_text, task=task, image_size=(image.width, image.height)
        )
        answer = parsed.get(task) if isinstance(parsed, dict) else None
        boxes = answer.get("bboxes") if isinstance(answer, dict) else []
        labels = answer.get("labels") if isinstance(answer, dict) else []
        matches: list[dict[str, Any]] = []
        for index, box in enumerate(boxes[:20] if isinstance(boxes, list) else []):
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                continue
            left, top, right, bottom = [float(value) for value in box]
            left = max(0.0, min(float(image.width), left))
            right = max(0.0, min(float(image.width), right))
            top = max(0.0, min(float(image.height), top))
            bottom = max(0.0, min(float(image.height), bottom))
            if right - left < 4 or bottom - top < 4:
                continue
            label = labels[index] if isinstance(labels, list) and index < len(labels) else query
            matches.append({
                "label": str(label)[:300],
                "box": [round(left), round(top), round(right), round(bottom)],
            })
        return matches, elapsed


class Handler(BaseHTTPRequestHandler):
    server_version = "JarvisFlorence/1.0"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"error": "not-found"})
            return
        grounder = self.server.grounder  # type: ignore[attr-defined]
        self._send(200, {
            "ready": True,
            "model": "microsoft/Florence-2-base-ft",
            "parameters": grounder.parameters,
            "device": "cpu",
            "privacy": "localhost-offline-memory-only",
        })

    def do_POST(self) -> None:
        if self.path != "/ground":
            self._send(404, {"error": "not-found"})
            return
        try:
            self.connection.settimeout(10.0)
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 14_000_000:
                raise ValueError("invalid-request-size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            query = str(payload.get("query") or "").strip()
            if not query or len(query) > 300:
                raise ValueError("invalid-query")
            image_bytes = base64.b64decode(str(payload.get("image") or ""), validate=True)
            if len(image_bytes) > 10_000_000:
                raise ValueError("image-too-large")
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            if image.width < 8 or image.height < 8 or image.width * image.height > 8_000_000:
                raise ValueError("invalid-image-dimensions")
            grounder = self.server.grounder  # type: ignore[attr-defined]
            matches, elapsed = grounder.ground(image, query)
            self._send(200, {"matches": matches, "inference_seconds": round(elapsed, 3)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)[:120]})
        except Exception as exc:
            self._send(500, {"error": type(exc).__name__})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument(
        "--model-dir", default=r"C:\Projects\JARVIS\models\florence-2-base-ft"
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Florence worker only permits localhost binding")
    started = time.perf_counter()
    grounder = FlorenceGrounder(Path(args.model_dir).resolve())
    # One request at a time is intentional: concurrent CPU inference would
    # multiply memory pressure and increase latency for every caller.
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    server.grounder = grounder  # type: ignore[attr-defined]
    print(
        f"Florence worker ready on 127.0.0.1:{args.port}; "
        f"parameters={grounder.parameters}; cold_start={time.perf_counter() - started:.3f}s",
        flush=True,
    )
    server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
