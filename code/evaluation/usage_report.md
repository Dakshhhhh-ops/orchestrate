# Token usage and cost

Final full-dataset run: 2026-10-24 15:45 UTC, 518 requests, statuses {'INCOMPLETE': 355, 'COMPLETE': 153, 'NOT_APPLICABLE': 10}.

## Model calls in this run

| Provider | Model | Calls | Input tokens | Output tokens | Total | Cost |
|---|---|---|---|---|---|---|
| Anthropic | - | 0 | 0 | 0 | 0 | $0.00 |

No `ANTHROPIC_API_KEY` was configured for this run, so intent came from the rule classifier and screenshot facts from `code/cache/image_facts.json`. Those facts were extracted once during development with `claude-opus-5-5` vision, and that interactive extraction is not metered here. Everything else in the pipeline is deterministic code with zero model tokens.

## Expected usage of a keyed run (estimate)

Pricing used: `claude-opus-5-5` $4.00 / $20.00 per million input / output tokens.

| Purpose | Calls | Input tokens | Output tokens | Est. cost (USD) |
|---|---|---|---|---|
| Intent classification (375 distinct texts, 40 per call) | 10 | 33,500 | 8,800 | $0.3100 |
| Screenshot facts | 11 | 22,550 | 7,150 | $0.2332 |
| **Total** | **21** | **56,050** | **15,950** | **$0.5432** |

- Average per request: 139 tokens, $0.001049

## Why the token bill is small

- The model is used only where language or pixels must be understood: request intent (any language, negations) and screenshots. Identity matching, evidence parsing, state reconstruction and planning are deterministic code.
- Identical request texts are de-duplicated before classification, and 40 are sent per call.
- Every reply is cached by request hash (`code/cache/llm/`), so re-runs cost nothing and are reproducible.
- Screenshot facts are extracted once per image, not once per case.
