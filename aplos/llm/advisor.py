"""LLM advisor: builds prompts, runs inference with a timeout, validates output.

The advisor never raises into the pipeline. Any failure (no model, timeout,
invalid JSON, schema violation) is returned as an error so the deterministic
engine decides instead.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

from aplos.config import env
from aplos.config.settings import LLMSettings
from aplos.llm.backends import (
    BackendUnavailableError,
    InferenceTimeoutError,
    LlamaCppBackend,
    LLMBackend,
    MockLLMBackend,
)
from aplos.llm.prompts import PROMPT_VERSION, build_prompt
from aplos.llm.schema import OUTPUT_JSON_SCHEMA, ModelOutputError, ModelRecommendation, parse_recommendation

log = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")
# Backstop beyond the backend's own deadline (llama.cpp checks it per token).
TIMEOUT_GRACE_S = 5.0


@dataclass
class AdvisorStatus:
    mode: str
    available: bool
    source: str
    detail: str


@dataclass
class AdvisorResult:
    recommendation: ModelRecommendation | None
    source: str
    error: str | None
    raw_output: str | None
    latency_ms: int
    attempts: int
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation": self.recommendation.model_dump(mode="json") if self.recommendation else None,
            "source": self.source,
            "error": self.error,
            "raw_output": (self.raw_output or "")[:2000] or None,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "prompt_version": self.prompt_version,
        }


class LLMAdvisor:
    def __init__(self, backend: LLMBackend | None, settings: LLMSettings, unavailable_reason: str | None = None) -> None:
        self.backend = backend
        self.settings = settings
        self.unavailable_reason = unavailable_reason

    def status(self) -> AdvisorStatus:
        if self.backend is None:
            return AdvisorStatus(self.settings.mode, False, "none", self.unavailable_reason or "Model disabled")
        return AdvisorStatus(self.settings.mode, True, self.backend.source, self.backend.description)

    def recommend(self, context: dict[str, Any], max_attempts: int = 2) -> AdvisorResult:
        if self.backend is None:
            return AdvisorResult(None, "none", self.unavailable_reason or "Model disabled", None, 0, 0)
        prompt = build_prompt(context)
        started = time.perf_counter()
        raw: str | None = None
        error: str | None = None
        attempts = 0
        for attempts in range(1, max_attempts + 1):
            try:
                future = _EXECUTOR.submit(
                    self.backend.generate,
                    prompt,
                    json_schema=OUTPUT_JSON_SCHEMA,
                    max_tokens=self.settings.max_tokens,
                    temperature=self.settings.temperature,
                    timeout_s=self.settings.timeout_s,
                    context=context,
                )
                raw = future.result(timeout=self.settings.timeout_s + TIMEOUT_GRACE_S)
                rec = parse_recommendation(raw)
                return AdvisorResult(rec, self.backend.source, None, raw, _ms(started), attempts)
            except ModelOutputError as exc:
                error = str(exc)  # malformed output: one retry
                log.warning("Invalid model output", extra={"ctx": {"attempt": attempts, "error": error}})
                continue
            except (FutureTimeout, InferenceTimeoutError):
                error = f"Model inference timed out after {self.settings.timeout_s:.0f}s"
            except BackendUnavailableError as exc:
                error = str(exc)
            except Exception as exc:  # inference must never crash the pipeline
                error = f"Model inference failed: {exc!r}"
                log.exception("Model inference failed")
            break
        return AdvisorResult(None, self.backend.source, error, raw, _ms(started), attempts)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def resolve_model_path(settings: LLMSettings) -> str:
    return str(env.resolve_project_path(env.model_path_override() or settings.model_path))


def build_advisor(settings: LLMSettings, preload: bool = False) -> LLMAdvisor:
    """Create the advisor for the configured mode.

    `local` only reports the model as available once the file exists and
    llama-cpp-python is importable; it is never silently replaced by the mock.
    """
    if settings.mode == "disabled":
        return LLMAdvisor(None, settings, "Model disabled in settings (deterministic rules only)")
    if settings.mode == "mock":
        return LLMAdvisor(MockLLMBackend(), settings)
    path = resolve_model_path(settings)
    backend = LlamaCppBackend(env.resolve_project_path(path), settings.n_ctx, settings.n_threads, settings.n_gpu_layers)
    if not backend.model_path.is_file():
        return LLMAdvisor(None, settings, f"Model file not found: {env.display_path(backend.model_path)}")
    if not LlamaCppBackend.library_available():
        return LLMAdvisor(None, settings, "llama-cpp-python is not installed")
    if preload:
        try:
            backend.load()
        except BackendUnavailableError as exc:
            return LLMAdvisor(None, settings, str(exc))
    return LLMAdvisor(backend, settings)
