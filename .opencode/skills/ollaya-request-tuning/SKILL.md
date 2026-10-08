---
name: ollaya-request-tuning
description: Use when tuning Ollaya questions/request wording against the target-labeled Ollaya training CSV, changing `ollaya_request`, creating a request version, or rolling back a request version.
---

# Ollaya request tuning

Tune Blot's Ollaya question payload against the labeled word CSV while keeping request changes auditable and reversible. At process startup, `backend/app/ollaya_scoring.py` loads the numerically highest `ollaya_request_vNNN.json` from `ollaya_requests/`. Its manifest version and questions become `OLLAYA_REQUEST_VERSION` and `_QUESTIONS` for that process.

## Target dataset

Use the supplied file by default:

```text
RAP_TTP_Procedure_Official_Closed_v1.0-draft09302100.docx.2a945e09.ollaya.csv
```

Its schema is:

```csv
word,is_identifier_percent,is_operationally_significant_percent,is_common_word_percent,target_is_identifier,target_is_operationally_significant,target_is_common_word
```

- Use the three `target_*` columns as gold labels; accept only `0`/`1` (or equivalent explicit boolean values). Never edit or infer target labels from model scores.
- The `*_percent` columns are prior Ollaya outputs, not labels. Treat blank scores as missing, not zero.
- This dataset has no context column. Score each example using only `{"candidate": "<word>"}`; do not fabricate context or imply that context-sensitive labels are validated from word-only input.
- Validate the header, row count, target completeness, duplicate words, and target distribution before scoring. The supplied baseline contains 2,879 rows, 10 identifier positives, 28 operational-significance positives, and 28 common-word negatives. Report class counts with every metric; overall accuracy alone is misleading.
- Treat the CSV and any Ollaya request/response logs as sensitive local data. Use the installed local Ollaya CLI only. Do not send the CSV, words, labels, or request log to a hosted prompt-revision service.

## Versioning and rollback rules

1. Read the current request version from the running scorer's `OLLAYA_REQUEST_VERSION` and inspect the matching immutable manifest before editing.
2. Never edit or replace an existing versioned snapshot. Create the next sequential version (for example, `ollaya_request_v002.json`) by copying the full prior manifest and changing only the necessary request fields and metadata. The scorer selects the highest numeric suffix, not the latest file modification time.
3. Keep the request wire shape stable: `model`, `transport`, `questions`, `response_contract`, and `training_csv`. The Ollaya CLI receives the `questions` object and a candidate `state`; version metadata is logged by Blot, not sent as an unknown Ollaya question field.
4. Do not edit `_QUESTIONS` or hard-code a version in Python. The manifest is the source of truth. Restart the backend after adding a version; the loaded version appears in scoring results, logs, and cache keys. Do not change `SCORING_METHOD` unless scoring semantics or result calculation also change.
5. Preserve independent binary labels, the existing Yes/No response contract, and the 0.5 decision threshold. Do not optimize aggregate accuracy by suppressing rare positive classes.
6. To roll back without modifying history, create a new highest-numbered manifest by copying the chosen prior request's contents but setting `version` to that new manifest's filename stem. Restart the backend and confirm the logged/result version and question content. This provides a new rollback event while preserving every prior snapshot.

## Tuning and evaluation procedure

1. Read `ollaya_requests/ollaya_request_v001.json` for the frozen baseline and compare it with the running version shown in scorer results/logs. Do not assume that v001 remains active.
2. Score the same CSV rows with the current request and the model tag read from the repository `.env` key `OLLAYA_MODEL`. Keep that exact model tag fixed across request-version comparisons. Preserve row order and gold columns in a separate evaluation output; never overwrite the supplied CSV.
3. Calculate per-signal confusion matrix, precision, recall, F1, balanced accuracy, false-positive/false-negative counts, and support. Explicitly show the scarce positive counts. Treat these as training-set results because the CSV is the tuning target; do not claim holdout/generalization performance.
4. Find specific false-positive/false-negative patterns from the per-word records and change only instructions/criteria that address those patterns. Do not remove the three signals, conflate their definitions, or change the model/threshold while attributing changes to request wording.
5. Create the next request snapshot before restarting the backend. Record the CSV SHA-256, model tag, baseline/new request versions, date, rationale, and affected error patterns in the new manifest or a companion evaluation record. Avoid embedding the complete dataset in request snapshots.
6. Re-run the identical rows and compare per-signal metrics, especially rare-positive recall and false positives. Reject or revise changes that improve aggregate accuracy only by losing rare positives. If no separate untouched holdout is available, state that the comparison is in-sample and do not present it as generalization evidence.
7. Restart the backend, then run `pytest backend/tests/test_ollaya_scoring.py`. Check that emitted result/log metadata names the highest version and that its loaded questions match that snapshot exactly.

## Request version manifest

`ollaya_requests/ollaya_request_v001.json` is the initial baseline. Each subsequent manifest must retain the same top-level structure and include:

- `version`: unique `ollaya_request_vNNN` identifier;
- model and CLI transport/format;
- complete ordered questions, instructions, and Yes/No criteria;
- the response contract and decision threshold;
- training CSV filename and target/input column mapping;
- a concise change rationale and evaluation provenance.

Keep old snapshots indefinitely. The scorer fails at startup if the highest-numbered manifest is malformed rather than silently falling back to an older request. A newly added manifest takes effect after the backend process restarts; a running process keeps its already-loaded request.
