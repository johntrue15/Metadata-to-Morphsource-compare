"""
Shared helpers for AutoResearchClaw scripts.

Centralizes dotenv loading, MorphoSource field parsing, LLM calls,
and configurable paths so they aren't duplicated across 15+ scripts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import importlib.util
from pathlib import Path
from typing import Any, List, Optional

log = logging.getLogger("AutoResearchClaw")

# ---------------------------------------------------------------------------
# Configurable paths (override via environment variables)
# ---------------------------------------------------------------------------

SLICER_BIN = os.environ.get(
    "SLICER_BIN", "/Applications/Slicer.app/Contents/MacOS/Slicer"
)

AUTORESEARCHCLAW_HOME = Path(
    os.environ.get("AUTORESEARCHCLAW_HOME", str(Path.home() / ".autoresearchclaw"))
)

MORPHOSOURCE_API_BASE = os.environ.get(
    "MORPHOSOURCE_API_BASE", "https://www.morphosource.org/api"
)

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Tiered model selection
# ---------------------------------------------------------------------------

# Three tiers: fast (cheap data gathering), mid (summaries), peak (deep reasoning)
# Override via OPENAI_MODEL_FAST, OPENAI_MODEL_MID, OPENAI_MODEL env vars

def _detect_model_family() -> str:
    """Detect model family from the peak model to align tiers."""
    peak = os.environ.get("OPENAI_MODEL", "gpt-5.4")
    if peak.startswith("gpt-5.4"):
        return "gpt-5.4"
    if peak.startswith("gpt-5"):
        return "gpt-5"
    if peak.startswith("gpt-4.1"):
        return "gpt-4.1"
    if peak.startswith("gpt-4o"):
        return "gpt-4o"
    return "gpt-4o"

_TIER_DEFAULTS = {
    "gpt-5.4": {"fast": "gpt-4.1-nano", "mid": "gpt-4.1-mini", "peak": "gpt-5.4"},
    "gpt-5":   {"fast": "gpt-4.1-nano", "mid": "gpt-4.1-mini", "peak": "gpt-5"},
    "gpt-4.1": {"fast": "gpt-4.1-nano", "mid": "gpt-4.1-mini", "peak": "gpt-4.1"},
    "gpt-4o":  {"fast": "gpt-4o-mini",  "mid": "gpt-4o-mini",  "peak": "gpt-4o"},
}


def get_model_for_tier(tier: str = "peak") -> str:
    """Get the model name for a given tier: 'fast', 'mid', or 'peak'.

    fast  -- cheap, high-speed: query decomposition, formatting, basic parsing
    mid   -- balanced: evaluation, summaries, search refinement
    peak  -- most capable: final synthesis, deep reasoning, protocol design
    """
    family = _detect_model_family()
    defaults = _TIER_DEFAULTS.get(family, _TIER_DEFAULTS["gpt-4o"])

    if tier == "fast":
        return os.environ.get("OPENAI_MODEL_FAST", defaults["fast"])
    elif tier == "mid":
        return os.environ.get("OPENAI_MODEL_MID", defaults["mid"])
    else:
        return os.environ.get("OPENAI_MODEL", defaults["peak"])


# ---------------------------------------------------------------------------
# Token usage tracking and cost estimation
# ---------------------------------------------------------------------------

# Pricing per 1M tokens (input/output) as of March 2026
# Update these when OpenAI changes pricing
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5": (5.00, 15.00),
    "gpt-5-mini": (1.25, 5.00),
    "gpt-5.4": (2.00, 8.00),
    "gpt-5.4-mini": (0.50, 2.00),
    "gpt-5.4-nano": (0.15, 0.60),
    "o1": (15.00, 60.00),
    "o3": (10.00, 40.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
}


class TokenTracker:
    """Tracks cumulative token usage and estimates cost across LLM calls."""

    def __init__(self):
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_calls: int = 0
        self.calls_by_label: dict[str, dict] = {}

    def record(self, label: str, prompt_tokens: int, completion_tokens: int, model: str):
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_calls += 1

        if label not in self.calls_by_label:
            self.calls_by_label[label] = {
                "calls": 0, "prompt": 0, "completion": 0
            }
        self.calls_by_label[label]["calls"] += 1
        self.calls_by_label[label]["prompt"] += prompt_tokens
        self.calls_by_label[label]["completion"] += completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    def estimate_cost(self, model: str = "") -> float:
        """Estimate total cost in USD based on model pricing."""
        model = model or get_openai_model()
        base = model.split("-202")[0]
        input_rate, output_rate = MODEL_PRICING.get(base, (2.00, 8.00))
        input_cost = (self.total_prompt_tokens / 1_000_000) * input_rate
        output_cost = (self.total_completion_tokens / 1_000_000) * output_rate
        return round(input_cost + output_cost, 4)

    def summary(self, model: str = "") -> str:
        """One-line summary: tokens and cost."""
        cost = self.estimate_cost(model)
        return (
            f"{self.total_calls} calls | "
            f"{self.total_prompt_tokens:,} prompt + "
            f"{self.total_completion_tokens:,} completion = "
            f"{self.total_tokens:,} tokens | "
            f"~${cost:.4f}"
        )

    def markdown_table(self, model: str = "") -> str:
        """Markdown table for GitHub issue rendering."""
        model = model or get_openai_model()
        base = model.split("-202")[0]
        input_rate, output_rate = MODEL_PRICING.get(base, (2.00, 8.00))
        cost = self.estimate_cost(model)

        lines = [
            "| Metric | Value |",
            "|--------|-------|",
            f"| Model | `{model}` |",
            f"| Total API calls | {self.total_calls} |",
            f"| Prompt tokens | {self.total_prompt_tokens:,} |",
            f"| Completion tokens | {self.total_completion_tokens:,} |",
            f"| Total tokens | {self.total_tokens:,} |",
            f"| Input rate | ${input_rate}/1M tokens |",
            f"| Output rate | ${output_rate}/1M tokens |",
            f"| **Estimated cost** | **${cost:.4f}** |",
        ]

        if self.calls_by_label:
            lines.append("")
            lines.append("| Stage | Calls | Prompt | Completion |")
            lines.append("|-------|------:|-------:|-----------:|")
            for label_key in sorted(self.calls_by_label.keys()):
                d = self.calls_by_label[label_key]
                clean_label = label_key.split("-")[0] if "-" in label_key else label_key
                lines.append(
                    f"| {clean_label} | {d['calls']} | "
                    f"{d['prompt']:,} | {d['completion']:,} |"
                )

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimate_cost(),
            "model": get_openai_model(),
            "calls_by_label": self.calls_by_label,
        }


# Global tracker instance
token_tracker = TokenTracker()


# ---------------------------------------------------------------------------
# .env loading (single implementation)
# ---------------------------------------------------------------------------


def load_dotenv(start_dir: Path | str | None = None) -> Optional[str]:
    """Walk up from *start_dir* to find a .env file and load it.

    Uses python-dotenv if installed, otherwise parses manually.
    Returns the path to the .env file found, or None.
    """
    try:
        from dotenv import load_dotenv as _load  # type: ignore

        _use_dotenv = True
    except ImportError:
        _load = None
        _use_dotenv = False

    search = Path(start_dir).resolve() if start_dir else SCRIPT_DIR
    for _ in range(6):
        env_file = search / ".env"
        if env_file.is_file():
            if _use_dotenv and _load:
                _load(env_file, override=False)
            else:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
            return str(env_file)
        search = search.parent
    return None


# ---------------------------------------------------------------------------
# MorphoSource field helpers
# ---------------------------------------------------------------------------


def safe_first(value: Any) -> str:
    """Safely get the first element from a MorphoSource API field value.

    MorphoSource wraps most values in single-element lists like ["Mesh"].
    An empty list [] returns "". A plain string/int is str()-ified.
    None returns "".
    """
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# OpenAI / LLM helpers
# ---------------------------------------------------------------------------

# Lazy-loaded OpenAI client class
_OpenAI = None


def _get_openai_class():
    global _OpenAI
    if _OpenAI is not None:
        return _OpenAI
    if "openai" in sys.modules:
        _OpenAI = getattr(sys.modules["openai"], "OpenAI", None)
    else:
        spec = importlib.util.find_spec("openai")
        if spec:
            from openai import OpenAI  # type: ignore
            _OpenAI = OpenAI
    return _OpenAI


def is_reasoning_model(model_name: str) -> bool:
    """Detect reasoning-family models that need max_completion_tokens
    and don't support temperature/response_format."""
    m = model_name.lower()
    return m.startswith(("o1", "o3", "o4", "gpt-5"))


def get_openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", "gpt-5.4")


def call_llm(
    messages: List[dict],
    max_tokens: int = 2000,
    json_mode: bool = False,
    label: str = "LLM",
    tier: str = "peak",
) -> Optional[str]:
    """Call OpenAI chat completions with tiered model selection.

    Tiers:
        fast  -- gpt-4.1-nano: query decomposition, formatting, parsing
        mid   -- gpt-4.1-mini: evaluation, summaries, refinement
        peak  -- gpt-5.4: final synthesis, deep reasoning, protocol design

    Returns the content string (may be empty) or None on hard failure.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("[%s] OPENAI_API_KEY not set", label)
        return None

    OpenAI = _get_openai_class()
    if OpenAI is None:
        log.warning("[%s] openai package not installed", label)
        return None

    model = get_model_for_tier(tier)
    client = OpenAI(api_key=api_key)
    reasoning = is_reasoning_model(model)

    kwargs: dict = {"model": model, "messages": messages}

    if reasoning:
        kwargs["max_completion_tokens"] = max_tokens
        if json_mode:
            msgs = list(messages)
            if msgs and msgs[0]["role"] == "system":
                msgs[0] = {
                    **msgs[0],
                    "content": msgs[0]["content"] + "\nRespond with valid JSON only.",
                }
            kwargs["messages"] = msgs
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0.7
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

    log.debug("[%s] model=%s reasoning=%s tokens=%d", label, model, reasoning, max_tokens)

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as first_err:
        err_str = str(first_err).lower()
        if not reasoning and ("max_tokens" in err_str or "unsupported" in err_str):
            log.info("[%s] Retrying as reasoning model", label)
            kwargs.pop("max_tokens", None)
            kwargs.pop("temperature", None)
            kwargs.pop("response_format", None)
            kwargs["max_completion_tokens"] = max_tokens
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as retry_err:
                log.error("[%s] Retry failed: %s", label, retry_err)
                return None
        else:
            log.error("[%s] API call failed: %s", label, first_err)
            return None

    choice = response.choices[0] if response.choices else None
    content = choice.message.content if choice else None
    usage = response.usage

    if usage:
        token_tracker.record(label, usage.prompt_tokens, usage.completion_tokens, model)

    log.info(
        "[%s] finish=%s len=%s usage=%s cost=$%s",
        label,
        choice.finish_reason if choice else "none",
        len(content) if content else 0,
        f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "N/A",
        f"{token_tracker.estimate_cost():.4f}",
    )

    return (content or "").strip()
