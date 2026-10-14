"""Inference backends. Model loading/inference lives here, separate from scheduling.

- `LlamaCppBackend`: real local inference of a Gemma 2B GGUF via llama-cpp-python.
- `MockLLMBackend`: DEVELOPMENT ONLY. Deterministic, no model involved; its
  output is labelled `mock` in every record and in the UI.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class BackendUnavailableError(RuntimeError):
    """The backend cannot run (model missing, library missing, failed to load)."""


class InferenceTimeoutError(RuntimeError):
    pass


class LLMBackend(Protocol):
    source: str  # value recorded as decision.model_source
    description: str

    def generate(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        timeout_s: float,
        context: dict[str, Any] | None = None,
    ) -> str: ...


def format_gemma_prompt(user_text: str) -> str:
    """Gemma 2 instruction-tuned chat template (no system role)."""
    return f"<start_of_turn>user\n{user_text}<end_of_turn>\n<start_of_turn>model\n"


class LlamaCppBackend:
    """Gemma 2B (GGUF) through llama.cpp. The model is loaded once and reused."""

    source = "gemma-2b-local"

    _lock = threading.Lock()
    _cache: dict[tuple[str, int, int, int], Any] = {}

    def __init__(self, model_path: Path, n_ctx: int = 2048, n_threads: int = 0, n_gpu_layers: int = 0) -> None:
        self.model_path = Path(model_path)
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers
        self.description = f"Gemma 2B via llama.cpp ({self.model_path.name})"
        self._llm: Any = None
        self.load_seconds: float | None = None

    @staticmethod
    def library_available() -> bool:
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            return False
        return True

    def load(self) -> None:
        if self._llm is not None:
            return
        if not self.model_path.is_file():
            raise BackendUnavailableError(f"Model file not found: {self.model_path}")
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise BackendUnavailableError(
                "llama-cpp-python is not installed (pip install -r requirements-llm.txt)"
            ) from exc
        key = (str(self.model_path.resolve()), self.n_ctx, self.n_threads, self.n_gpu_layers)
        with self._lock:
            if key not in self._cache:
                started = time.perf_counter()
                try:
                    self._cache[key] = Llama(
                        model_path=str(self.model_path),
                        n_ctx=self.n_ctx,
                        n_threads=self.n_threads or None,
                        n_gpu_layers=self.n_gpu_layers,
                        verbose=False,
                    )
                except Exception as exc:  # llama.cpp raises ValueError/RuntimeError on bad files
                    raise BackendUnavailableError(f"Failed to load model: {exc}") from exc
                self.load_seconds = round(time.perf_counter() - started, 1)
                log.info("Local model loaded", extra={"ctx": {"model": self.model_path.name, "s": self.load_seconds}})
            self._llm = self._cache[key]

    @property
    def loaded(self) -> bool:
        return self._llm is not None

    def generate(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        timeout_s: float,
        context: dict[str, Any] | None = None,
    ) -> str:
        self.load()
        deadline = time.monotonic() + timeout_s
        timed_out = False

        def stop_on_deadline(_ids: Any, _logits: Any) -> bool:
            nonlocal timed_out
            if time.monotonic() > deadline:
                timed_out = True
                return True
            return False

        kwargs: dict[str, Any] = {"max_tokens": max_tokens, "temperature": temperature, "stop": ["<end_of_turn>"]}
        try:
            from llama_cpp import StoppingCriteriaList

            kwargs["stopping_criteria"] = StoppingCriteriaList([stop_on_deadline])
        except ImportError:
            pass  # the advisor's thread timeout still applies
        try:
            from llama_cpp import LlamaGrammar

            kwargs["grammar"] = LlamaGrammar.from_json_schema(json.dumps(json_schema), verbose=False)
        except Exception as exc:  # grammar support varies between versions; schema validation still applies
            log.warning("JSON grammar unavailable; relying on schema validation", extra={"ctx": {"error": str(exc)}})
        with self._lock:  # llama.cpp contexts are not thread-safe
            out = self._llm.create_completion(format_gemma_prompt(prompt), **kwargs)
        if timed_out:
            raise InferenceTimeoutError(f"Inference exceeded {timeout_s:.0f}s")
        return str(out["choices"][0]["text"])


class MockLLMBackend:
    """DEVELOPMENT ONLY: echoes the rule engine's default as model-shaped JSON."""

    source = "mock"
    description = "Deterministic mock (no language model)"

    def generate(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        timeout_s: float,
        context: dict[str, Any] | None = None,
    ) -> str:
        if not context:
            raise BackendUnavailableError("Mock backend requires structured context")
        re_ = context["rule_engine"]
        action = re_["default_action"]
        return json.dumps({
            "action": action,
            "reason": f"[MOCK] Rule-engine default echoed for development: {'; '.join(re_['findings'])[:400]}",
            "conditions": re_["findings"][:4],
            "confidence": 0.7,
            "next_cleaning_time": re_["suggested_reschedule_local"] if action == "POSTPONE" else None,
        })


class ScriptedBackend:
    """Test helper: returns pre-set outputs (or raises) in order."""

    def __init__(self, outputs: list[str | Exception], source: str = "scripted-test") -> None:
        self.outputs = list(outputs)
        self.source = source
        self.description = "Scripted test backend"
        self.calls = 0

    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.calls += 1
        item = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(item, Exception):
            raise item
        if item == "__sleep__":
            time.sleep(kwargs.get("timeout_s", 1) + 0.5)
            return "{}"
        return item
