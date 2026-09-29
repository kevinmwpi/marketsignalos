# Market-matching eval set

Labeled Polymarket ↔ Kalshi pairs used to measure the market matcher. The labeling
rule, the design choices and the commands are in
[docs/llm-judge.md](../../docs/llm-judge.md). Read
[what "same event" means](../../docs/llm-judge.md#what-same-event-means) before labeling.

| File | Written by | Contents |
|---|---|---|
| `cases.jsonl` | `seed`, `label` | Labeled cases only; a malformed line rejects the whole file |
| `to_label.jsonl` | `sample` | Unlabeled queue (`label: null`) |
| `to_label.manifest.json` | `sample` | Seed, quotas, input hashes and matcher settings for the queue |
| `reports/` | `run` | Eval reports; `tfidf-baseline.*` is the baseline |

Labels are written only by a person, through `label` or an earlier `review-matches`
decision imported by `seed`. No command writes a label on its own.
