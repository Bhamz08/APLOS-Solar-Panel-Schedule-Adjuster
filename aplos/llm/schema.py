"""Schema for model output and robust parsing of raw model text."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from aplos.models import Action


class ModelOutputError(ValueError):
    pass


class ModelRecommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Action
    reason: str = Field(min_length=3, max_length=600)
    conditions: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0, le=1)
    next_cleaning_time: str | None = Field(None, description="Local time YYYY-MM-DDTHH:MM, or null")

    @field_validator("action", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("conditions", mode="before")
    @classmethod
    def _conditions(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [v]
        return [str(x)[:160] for x in v] if isinstance(v, list) else v

    @field_validator("next_cleaning_time", mode="before")
    @classmethod
    def _blank(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"):
            return None
        return v

    def next_time_utc(self, tz: ZoneInfo) -> datetime | None:
        """Parse `next_cleaning_time` (site-local unless an offset is given)."""
        if not self.next_cleaning_time:
            return None
        try:
            dt = datetime.fromisoformat(self.next_cleaning_time.strip().replace(" ", "T"))
        except ValueError as exc:
            raise ModelOutputError(f"Unparseable next_cleaning_time {self.next_cleaning_time!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(ZoneInfo("UTC"))


# JSON schema handed to llama.cpp for grammar-constrained decoding.
OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["CLEAN", "POSTPONE", "SKIP"]},
        "reason": {"type": "string"},
        "conditions": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "next_cleaning_time": {"type": ["string", "null"]},
    },
    "required": ["action", "reason", "conditions", "confidence", "next_cleaning_time"],
}

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of model text (handles code fences and chatter)."""
    if not text or not text.strip():
        raise ModelOutputError("Model returned empty output")
    fenced = _FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start < 0:
        raise ModelOutputError("No JSON object in model output")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(candidate[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise ModelOutputError(f"Invalid JSON from model: {exc}") from exc
                if not isinstance(obj, dict):
                    raise ModelOutputError("Model JSON is not an object")
                return obj
    raise ModelOutputError("Unterminated JSON object in model output")


def parse_recommendation(text: str) -> ModelRecommendation:
    data = extract_json(text)
    try:
        return ModelRecommendation.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ModelOutputError(f"Model output failed schema validation: {problems}") from exc
