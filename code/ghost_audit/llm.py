"""Claude client wrapper with on-disk caching and token accounting.

- Uses the official `anthropic` Python SDK. Credentials come from the environment
  (ANTHROPIC_API_KEY or an `ant auth login` profile); nothing is read from repo files.
- Every call is cached by a hash of the request under code/cache/llm/, so re-runs are
  deterministic and free. The usage ledger records fresh calls and cache hits separately.
- If the SDK or credentials are missing, callers fall back to their deterministic path.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "llm"

# USD per million tokens (input, output) - Anthropic first-party list prices.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
MODEL = os.environ.get("GHOST_MODEL", "claude-opus-5-5")
VISION_MODEL = os.environ.get("GHOST_VISION_MODEL", MODEL)


class Usage:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, model: str, purpose: str, inp: int, out: int, cached: bool):
        self.rows.append({"model": model, "purpose": purpose, "input_tokens": inp,
                          "output_tokens": out, "cached": cached})

    def summary(self) -> dict:
        per: dict[str, dict] = {}
        for r in self.rows:
            p = per.setdefault(r["model"], {"calls": 0, "fresh_calls": 0, "cache_hits": 0,
                                            "input_tokens": 0, "output_tokens": 0, "purposes": {}})
            p["calls"] += 1
            p["cache_hits"] += int(r["cached"])
            p["fresh_calls"] += int(not r["cached"])
            p["input_tokens"] += r["input_tokens"]
            p["output_tokens"] += r["output_tokens"]
            p["purposes"][r["purpose"]] = p["purposes"].get(r["purpose"], 0) + 1
        for m, p in per.items():
            pi, po = PRICES.get(m, (0.0, 0.0))
            p["est_cost_usd"] = round(p["input_tokens"] / 1e6 * pi + p["output_tokens"] / 1e6 * po, 6)
        return per


USAGE = Usage()
_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            import anthropic  # type: ignore
        except ImportError:
            return None
        try:
            _client = anthropic.Anthropic(max_retries=4)
        except Exception:
            return None
    return _client


def available() -> bool:
    if os.environ.get("GHOST_OFFLINE"):
        return False
    return _get_client() is not None and bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE")
    )


def _key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def call(system: str, content: list | str, purpose: str, model: str | None = None,
         max_tokens: int = 2048, effort: str = "low") -> str | None:
    """Return the text reply, or None when there is no cache entry and no usable client."""
    model = model or MODEL
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    payload = {"model": model, "max_tokens": max_tokens, "system": system, "effort": effort,
               "messages": [{"role": "user", "content": content}]}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = CACHE_DIR / f"{_key(payload)}.json"
    if cpath.exists():
        rec = json.loads(cpath.read_text(encoding="utf-8"))
        USAGE.add(rec.get("model", model), purpose, rec.get("input_tokens", 0), rec.get("output_tokens", 0), True)
        return rec["text"]
    if not available():
        return None
    import anthropic  # type: ignore

    client = _get_client()
    kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                  messages=payload["messages"], output_config={"effort": effort})
    try:
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:
        # older SDK without the fallbacks parameter
        resp = client.messages.create(**kwargs)
    except anthropic.BadRequestError:
        resp = client.messages.create(**kwargs)
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
        print(f"[llm] {purpose}: API error {type(exc).__name__}; using deterministic fallback")
        return None
    if getattr(resp, "stop_reason", None) == "refusal":
        return None
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    u = resp.usage
    rec = {"model": model, "purpose": purpose, "text": text,
           "input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0}
    cpath.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    USAGE.add(model, purpose, rec["input_tokens"], rec["output_tokens"], False)
    return text


def image_block(path: Path) -> dict:
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(path.read_bytes()).decode("utf-8")}}


def parse_json_reply(text: str | None):
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                continue
    return None
