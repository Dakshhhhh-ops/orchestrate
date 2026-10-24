"""Model client wrapper (Anthropic or Groq) with on-disk caching and token accounting.

- Providers: Anthropic through the official `anthropic` SDK (ANTHROPIC_API_KEY), or Groq through
  its OpenAI-compatible HTTP API (GROQ_API_KEY, standard library only). GHOST_PROVIDER picks one
  explicitly; otherwise Anthropic is used when its key is set, else Groq.
- Credentials come from the environment. An uncommitted `.env` file at the repository root is
  read at import for keys that are not already set; it is git-ignored and never packaged.
- Every call is cached by a hash of the request under code/cache/llm/, so re-runs are
  deterministic and free. The usage ledger records fresh calls and cache hits separately.
- If credentials are missing or the API fails, callers fall back to their deterministic path.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "llm"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from an uncommitted .env file; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


_load_dotenv()

# USD per million tokens (input, output). Anthropic first-party list prices; Groq prices from
# console.groq.com/docs/models (October 2026).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}


def _provider() -> str:
    p = os.environ.get("GHOST_PROVIDER", "").strip().lower()
    if p in ("anthropic", "groq"):
        return p
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return "anthropic"


PROVIDER = _provider()
MODEL = os.environ.get("GHOST_MODEL", "openai/gpt-oss-120b" if PROVIDER == "groq" else "claude-opus-5-5")
# screenshots: the cached facts were read with Claude vision; Groq lists no vision model here
VISION_MODEL = os.environ.get("GHOST_VISION_MODEL", "claude-opus-5-5")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def provider_of(model: str) -> str:
    return "Anthropic" if model.startswith("claude") else "Groq"


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
    if PROVIDER == "groq":
        return bool(os.environ.get("GROQ_API_KEY"))
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
    if PROVIDER == "groq" and not model.startswith("claude"):
        got = _groq_call(model, system, content, max_tokens, effort, purpose)
        if got is None:
            return None
        text, inp, out = got
        rec = {"model": model, "purpose": purpose, "text": text, "input_tokens": inp, "output_tokens": out}
        cpath.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        USAGE.add(model, purpose, inp, out, False)
        return text
    if PROVIDER == "groq":
        return None  # a Claude-only request (screenshots) without an Anthropic key: use the cache path
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


def _openai_content(content: list):
    """Anthropic-style content blocks -> OpenAI chat content (plain string when text only)."""
    parts = []
    for blk in content:
        if blk.get("type") == "image":
            src = blk["source"]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
        else:
            parts.append({"type": "text", "text": blk.get("text", "")})
    if all(p["type"] == "text" for p in parts):
        return "\n\n".join(p["text"] for p in parts)
    return parts


def _groq_call(model: str, system: str, content: list, max_tokens: int, effort: str, purpose: str):
    """One Groq chat completion -> (text, input_tokens, output_tokens), or None on failure."""
    body = {"model": model, "temperature": 0, "max_completion_tokens": max(max_tokens, 8192),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": _openai_content(content)}]}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "low"
    if "JSON" in system:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": "Bearer " + os.environ["GROQ_API_KEY"], "Content-Type": "application/json",
               "User-Agent": "ghost-audit/1.0"}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for attempt in range(6):
        req = urllib.request.Request(GROQ_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            text = out["choices"][0]["message"].get("content") or ""
            u = out.get("usage", {})
            return text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < 5:
                wait = exc.headers.get("retry-after") or ""
                try:
                    delay = min(float(wait), 60.0)
                except ValueError:
                    delay = 2.0 ** (attempt + 1)
                time.sleep(delay)
                continue
            # never print the request itself: its headers carry the key
            print(f"[llm] {purpose}: Groq HTTP {exc.code}; using deterministic fallback")
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            if attempt < 5:
                time.sleep(2.0 ** (attempt + 1))
                continue
            print(f"[llm] {purpose}: Groq request failed ({type(exc).__name__}); using deterministic fallback")
            return None
    return None


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
