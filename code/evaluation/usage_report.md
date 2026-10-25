# Token usage and cost

Final full-dataset run: 2026-10-25 13:01 UTC, 518 requests, statuses {'INCOMPLETE': 355, 'COMPLETE': 153, 'NOT_APPLICABLE': 10}.

## Model calls in this run

| Provider | Model | Calls (fresh / cache hits) | Input tokens | Output tokens | Total tokens | Est. cost (USD) | Purposes |
|---|---|---|---|---|---|---|---|
| Groq | `openai/gpt-oss-120b` | 11 (0 / 11) | 23,301 | 7,542 | 30,843 | $0.0080 | intent: 10, intent_tiebreak: 1 |
| **All models** | | **11** | **23,301** | **7,542** | **30,843** | **$0.0080** | |

- Prices used (USD per million input / output tokens): `openai/gpt-oss-120b` $0.150 / $0.60 (Groq: console.groq.com/docs/models; Anthropic list prices).
- The intent batches cover every distinct request text in the 518 evaluation requests and the 32 public samples, which the run audits together; averages are over the 518 requests.
- Average tokens per request: 59.5
- Average cost per request: $0.000015
- Token counts are the API's `usage` figures recorded when each reply was first produced; cache hits replay the same figures so the totals describe what the run consumed.
- Screenshot facts are not in this table: they come from `code/cache/image_facts.json`, read once during development with `claude-opus-5-5` vision (not metered here); `--refresh-vision` with an Anthropic key re-reads them.

## Expected usage of a keyed run (estimate)

Pricing used: intent `openai/gpt-oss-120b` $0.150 / $0.60, screenshots `claude-opus-5-5` $4.00 / $20.00 per million input / output tokens.

| Purpose | Model | Calls | Input tokens | Output tokens | Est. cost (USD) |
|---|---|---|---|---|---|
| Intent classification (375 distinct texts, 40 per call) | `openai/gpt-oss-120b` | 10 | 33,500 | 8,800 | $0.0103 |
| Screenshot facts | `claude-opus-5-5` | 11 | 22,550 | 7,150 | $0.2332 |
| **Total** | | **21** | **56,050** | **15,950** | **$0.2435** |

- Average per request: 139 tokens, $0.000470

## Why the token bill is small

- The model is used only where language or pixels must be understood: request intent (any language, negations) and screenshots. Identity matching, evidence parsing, state reconstruction and planning are deterministic code.
- Identical request texts are de-duplicated before classification, and 40 are sent per call.
- Every reply is cached by request hash (`code/cache/llm/`), so re-runs cost nothing and are reproducible.
- Screenshot facts are extracted once per image, not once per case.
