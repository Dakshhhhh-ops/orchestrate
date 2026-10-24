"""Write code/evaluation/usage_report.md from the token ledger of the run that produced output.csv."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import llm


def estimate_offline(n_requests: int, n_distinct_texts: int, n_images: int) -> dict:
    """Token estimate for a keyed run, from measured prompt sizes (chars/4) - used only when no calls ran."""
    intent_batches = -(-n_distinct_texts // 40)
    intent_in = intent_batches * (350 + 40 * 75)      # system prompt + 40 requests of ~75 tokens
    intent_out = intent_batches * (40 * 22)            # one short JSON label per request
    vision_in = n_images * (1600 + 450)                # ~1.6k tokens per 1800x1150 screenshot + prompt
    vision_out = n_images * 650
    return {"intent": (intent_batches, intent_in, intent_out), "vision": (n_images, vision_in, vision_out)}


def write(path: Path, n_requests: int, n_distinct_texts: int, n_images: int, status_counts: dict):
    usage = llm.USAGE.summary()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Token usage and cost", "",
             f"Final full-dataset run: {now}, {n_requests} requests, statuses {status_counts}.", ""]
    fresh = sum(p["fresh_calls"] for p in usage.values()) if usage else 0
    if usage:
        lines += ["## Model calls in this run", "",
                  "| Provider | Model | Calls (fresh / cache hits) | Input tokens | Output tokens | Total tokens | Est. cost (USD) | Purposes |",
                  "|---|---|---|---|---|---|---|---|"]
        tot_in = tot_out = tot_cost = tot_calls = 0
        for m, p in sorted(usage.items()):
            tot = p["input_tokens"] + p["output_tokens"]
            lines.append(f"| Anthropic | `{m}` | {p['calls']} ({p['fresh_calls']} / {p['cache_hits']}) | {p['input_tokens']:,} | "
                         f"{p['output_tokens']:,} | {tot:,} | ${p['est_cost_usd']:.4f} | "
                         f"{', '.join(f'{k}: {v}' for k, v in p['purposes'].items())} |")
            tot_in += p["input_tokens"]
            tot_out += p["output_tokens"]
            tot_cost += p["est_cost_usd"]
            tot_calls += p["calls"]
        lines += [f"| **All models** | | **{tot_calls}** | **{tot_in:,}** | **{tot_out:,}** | **{tot_in + tot_out:,}** | "
                  f"**${tot_cost:.4f}** | |", "",
                  f"- Average tokens per request: {(tot_in + tot_out) / max(n_requests, 1):,.1f}",
                  f"- Average cost per request: ${tot_cost / max(n_requests, 1):.6f}",
                  "- Token counts are the API's `usage` figures recorded when each reply was first produced; "
                  "cache hits replay the same figures so the totals describe what the run consumed.", ""]
    if not fresh:
        est = estimate_offline(n_requests, n_distinct_texts, n_images)
        pi, po = llm.PRICES[llm.MODEL]
        ib, ii, io = est["intent"]
        vn, vi, vo = est["vision"]
        cost = (ii + vi) / 1e6 * pi + (io + vo) / 1e6 * po
        if not usage:
            lines += ["## Model calls in this run", "",
                      "| Provider | Model | Calls | Input tokens | Output tokens | Total | Cost |",
                      "|---|---|---|---|---|---|---|",
                      "| Anthropic | - | 0 | 0 | 0 | 0 | $0.00 |", "",
                      "No `ANTHROPIC_API_KEY` was configured for this run, so intent came from the rule "
                      "classifier and screenshot facts from `code/cache/image_facts.json`. Those facts were "
                      f"extracted once during development with `{llm.VISION_MODEL}` vision, and that "
                      "interactive extraction is not metered here. Everything else in the pipeline is "
                      "deterministic code with zero model tokens.", ""]
        lines += ["## Expected usage of a keyed run (estimate)", "",
                  f"Pricing used: `{llm.MODEL}` ${pi:.2f} / ${po:.2f} per million input / output tokens.", "",
                  "| Purpose | Calls | Input tokens | Output tokens | Est. cost (USD) |", "|---|---|---|---|---|",
                  f"| Intent classification ({n_distinct_texts} distinct texts, 40 per call) | {ib} | {ii:,} | {io:,} | ${(ii / 1e6 * pi + io / 1e6 * po):.4f} |",
                  f"| Screenshot facts | {vn} | {vi:,} | {vo:,} | ${(vi / 1e6 * pi + vo / 1e6 * po):.4f} |",
                  f"| **Total** | **{ib + vn}** | **{ii + vi:,}** | **{io + vo:,}** | **${cost:.4f}** |", "",
                  f"- Average per request: {(ii + vi + io + vo) / max(n_requests, 1):,.0f} tokens, ${cost / max(n_requests, 1):.6f}", ""]
    lines += ["## Why the token bill is small", "",
              "- The model is used only where language or pixels must be understood: request intent "
              "(any language, negations) and screenshots. Identity matching, evidence parsing, state "
              "reconstruction and planning are deterministic code.",
              "- Identical request texts are de-duplicated before classification, and 40 are sent per call.",
              "- Every reply is cached by request hash (`code/cache/llm/`), so re-runs cost nothing and are reproducible.",
              "- Screenshot facts are extracted once per image, not once per case.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
