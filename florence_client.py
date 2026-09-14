"""Small localhost-only client for JARVIS's isolated Florence worker."""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class FlorenceClient:
    def __init__(self, endpoint: str = "http://127.0.0.1:8795") -> None:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 8795:
            raise ValueError("Florence endpoint must be localhost HTTP port 8795")
        self.endpoint = endpoint.rstrip("/")
        self.enabled = os.environ.get("JARVIS_FLORENCE_ENABLED", "").strip() == "1"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def health(self, timeout: float = 0.4) -> dict[str, Any]:
        if not self.enabled:
            return {"ready": False, "reason": "disabled"}
        try:
            request = urllib.request.Request(self.endpoint + "/health", method="GET")
            with self._opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read(64_000).decode("utf-8"))
            return payload if isinstance(payload, dict) else {"ready": False}
        except (OSError, ValueError, urllib.error.URLError):
            return {"ready": False, "reason": "worker-unavailable"}

    def ground(self, image: Any, query: str, timeout: float = 15.0) -> list[dict[str, Any]]:
        if not self.enabled or image is None or not query.strip():
            return []
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "PNG", optimize=True)
        raw_image = buffer.getvalue()
        if len(raw_image) > 10_000_000:
            return []
        body = json.dumps({
            "query": query[:300],
            "image": base64.b64encode(raw_image).decode("ascii"),
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + "/ground",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read(256_000).decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return []
        matches = payload.get("matches") if isinstance(payload, dict) else None
        return matches[:20] if isinstance(matches, list) else []
