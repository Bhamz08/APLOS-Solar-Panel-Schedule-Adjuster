import json
import time

import pytest

from aplos.config.settings import LLMSettings
from aplos.engine.rules import evaluate_rules
from aplos.llm import advisor as advisor_mod
from aplos.llm.advisor import LLMAdvisor, build_advisor
from aplos.llm.backends import BackendUnavailableError, InferenceTimeoutError, MockLLMBackend, ScriptedBackend
from aplos.llm.prompts import build_context, build_prompt
from aplos.llm.schema import ModelOutputError, extract_json, parse_recommendation
from aplos.models import Action
from tests.helpers import event, rain_bundle

VALID = {"action": "POSTPONE", "reason": "Rain expected.", "conditions": ["6 mm"], "confidence": 0.8,
         "next_cleaning_time": "2026-03-12T06:00"}


def test_parse_valid_and_fenced():
    assert parse_recommendation(json.dumps(VALID)).action == Action.POSTPONE
    fenced = "Sure!\n```json\n" + json.dumps({**VALID, "action": "clean", "next_cleaning_time": "null"}) + "\n```"
    rec = parse_recommendation(fenced)
    assert rec.action == Action.CLEAN and rec.next_cleaning_time is None


@pytest.mark.parametrize(
    "text",
    ["", "no json here", '{"action": "POSTPONE", "reason": "x"', '{"action": "WAIT", "reason": "abc", "confidence": 0.5}',
     json.dumps({**VALID, "confidence": 3})],
)
def test_invalid_output_rejected(text):
    with pytest.raises(ModelOutputError):
        parse_recommendation(text)


def test_extract_json_handles_braces_in_strings():
    assert extract_json('x {"a": "}{", "b": 1} y') == {"a": "}{", "b": 1}


def test_prompt_contains_rules_and_constraints(settings):
    ev = evaluate_rules(event(), rain_bundle(), settings)
    ctx = build_context(event(), rain_bundle(), ev, settings, "FRESH", 0.5)
    prompt = build_prompt(ctx)
    assert '"allowed_actions":["POSTPONE","SKIP"]' in prompt
    assert "planned_cleaning_local" in prompt and "blocks_3h" in prompt
    assert len(prompt) < 6000  # fits a 2048-token context with room for output


def _ctx(settings):
    return build_context(event(), rain_bundle(), evaluate_rules(event(), rain_bundle(), settings), settings, "FRESH", 0.1)


def test_advisor_retries_invalid_json_once(settings):
    backend = ScriptedBackend(["garbage", json.dumps(VALID)])
    res = LLMAdvisor(backend, settings.llm).recommend(_ctx(settings))
    assert res.recommendation is not None and res.attempts == 2


def test_advisor_reports_persistent_invalid_json(settings):
    res = LLMAdvisor(ScriptedBackend(["garbage"]), settings.llm).recommend(_ctx(settings))
    assert res.recommendation is None and "No JSON" in res.error


def test_advisor_backend_errors(settings):
    for exc, text in [(BackendUnavailableError("Model file not found"), "not found"),
                      (InferenceTimeoutError("slow"), "timed out"), (RuntimeError("boom"), "failed")]:
        res = LLMAdvisor(ScriptedBackend([exc]), settings.llm).recommend(_ctx(settings))
        assert res.recommendation is None and text in res.error


def test_advisor_thread_timeout(settings, monkeypatch):
    monkeypatch.setattr(advisor_mod, "TIMEOUT_GRACE_S", 0.0)
    llm = LLMSettings.model_construct(**{**settings.llm.model_dump(), "timeout_s": 0.2})

    class Slow:
        source, description = "slow", "slow"

        def generate(self, prompt, **kw):
            time.sleep(0.6)
            return json.dumps(VALID)

    res = LLMAdvisor(Slow(), llm).recommend(_ctx(settings))
    assert res.recommendation is None and "timed out" in res.error
    time.sleep(0.5)  # let the worker finish before other tests use the executor


def test_mock_backend_is_labelled(settings):
    res = LLMAdvisor(MockLLMBackend(), settings.llm).recommend(_ctx(settings))
    assert res.source == "mock"
    assert res.recommendation.reason.startswith("[MOCK]")


def test_local_mode_without_model_is_unavailable(tmp_path):
    adv = build_advisor(LLMSettings(mode="local", model_path=str(tmp_path / "missing.gguf")))
    st = adv.status()
    assert not st.available and "not found" in st.detail
    assert adv.backend is None  # never silently swapped for the mock


def test_disabled_mode():
    assert not build_advisor(LLMSettings(mode="disabled")).status().available
