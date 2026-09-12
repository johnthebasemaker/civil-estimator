"""Minimal Ollama HTTP client.

Handoff §9: local only, no cloud APIs. Handoff §4: POST /api/generate with
model / prompt / images / format=json / stream=false.

Deliberately stdlib-only (urllib, not requests) so Phase 2 adds zero new
dependencies to requirements.txt.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("CIVIL_VISION_MODEL", "qwen2.5vl:7b")

# A single A0 tile takes ~50 s on an M-series MacBook Air; the machine keeps one
# warm model at a time, so a cold start adds the 6 GB load on top. 600 s of
# headroom means a slow first call fails the run only when something is really
# wrong.
DEFAULT_TIMEOUT_S = int(os.environ.get("CIVIL_VISION_TIMEOUT", "600"))


class OllamaError(RuntimeError):
    """Raised when the model host is unreachable or returns an unusable reply."""


@dataclass
class GenerateResult:
    response: str
    elapsed_s: float
    prompt_eval_count: int = 0
    eval_count: int = 0
    raw: dict = field(default_factory=dict)

    def json(self) -> dict:
        """Parse the response body as JSON.

        format="json" constrains Ollama's sampler to emit valid JSON, but a
        model that runs out of `num_predict` mid-object still returns a
        truncated string, so this stays defensive.
        """
        text = (self.response or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = _salvage_json(text)
        return parsed if isinstance(parsed, dict) else {}


def _salvage_json(text: str) -> dict:
    """Best-effort recovery of the outermost {...} from a chatty/truncated reply."""
    start = text.find("{")
    if start == -1:
        return {}
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start=start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


class OllamaClient:
    """Thin wrapper over /api/generate and /api/tags."""

    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                 timeout_s: int = DEFAULT_TIMEOUT_S):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    # ---------- health ----------
    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=10) as r:
                data = json.load(r)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise OllamaError(
                f"Cannot reach Ollama at {self.host}: {e}. "
                f"Start it with `ollama serve`."
            ) from e
        return [m.get("name", "") for m in data.get("models", [])]

    def health(self) -> tuple[bool, str]:
        """(ok, human-readable message). Never raises — used by the Streamlit UI."""
        try:
            names = self.list_models()
        except OllamaError as e:
            return False, str(e)
        if self.model not in names:
            return False, (f"Model {self.model!r} not pulled. Available: "
                           f"{', '.join(names) or '(none)'}. "
                           f"Run `ollama pull {self.model}`.")
        return True, f"{self.model} ready at {self.host}"

    # ---------- generation ----------
    def generate(self, prompt: str, images_b64: list[str] | None = None, *,
                 json_mode: bool = True, temperature: float = 0.0,
                 num_predict: int = 1024, retries: int = 1) -> GenerateResult:
        """One /api/generate call.

        temperature=0.0 by default: this is a transcription task, not a creative
        one, and a deterministic decode makes the extraction reproducible enough
        to be worth saving as a RAG example.
        """
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if images_b64:
            payload["images"] = images_b64
        if json_mode:
            payload["format"] = "json"

        body = json.dumps(payload).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            started = time.time()
            try:
                req = urllib.request.Request(
                    f"{self.host}/api/generate", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    data = json.load(r)
                return GenerateResult(
                    response=data.get("response", ""),
                    elapsed_s=round(time.time() - started, 2),
                    prompt_eval_count=data.get("prompt_eval_count", 0),
                    eval_count=data.get("eval_count", 0),
                    raw=data,
                )
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
                last_err = e
                if attempt < retries:
                    time.sleep(2.0)
        raise OllamaError(f"Ollama /api/generate failed after "
                          f"{retries + 1} attempt(s): {last_err}")
