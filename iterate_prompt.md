# Iterative Prompt Evaluation Plan

**Status:** Implemented as the interactive `backend.app.prompt_iteration` CLI and Python workflow.

**Goal:** Improve Ollaya's word-level classifications by reviewing errors, revising the scoring prompt with OpenAI GPT-6 Luna, and rerunning the same evaluation set.

## Overview

Each run uses a versioned prompt and a fixed document/input set. The pipeline extracts unique words, scores three independent signals, lets a user correct errors, and records whether the next prompt version reduces the number of misclassified words.

```text
Fixed source text
    → unique words
    → Ollaya scoring with prompt v001
    → run001.csv
    → human review and corrected labels
    → count errors and revise prompt to v002
    → rerun the same inputs and compare
```

The three signals are independent: a word can be both common and an identifier or operationally significant. Scores are percentages, not guarantees. Ollaya scoring runs locally. When an error is reviewed, its word, bounded text context, predicted labels, and corrected labels are sent to the configured OpenAI model to revise the prompt.

## Stage 0 — Freeze the evaluation run

Before scoring, record:

- Run ID, prompt version, exact prompt text, Ollaya model/version, and scoring date.
- The source document/version and the unique-word extraction/normalization rules.
- A fixed evaluation set. Reuse the same word set and contexts on each prompt version so the comparison is meaningful.
- The rule used to convert each percentage to a predicted Yes/No label. Default: `>= 50%` is Yes and `< 50%` is No; keep this threshold fixed across prompt versions.

Use stable, reproducible run IDs: `run001`, `run002`, and so on. Never overwrite an earlier prompt or run result.

## Stage 1 — Extract unique words

1. Convert supported editable document content to plain text locally before tokenization. Use the document adapters for DOCX, PPTX, XLSX, CSV and text-based formats; do not classify unsupported Office parts or imply that image/OCR text was extracted.
2. Extract words from the converted plain text using a documented tokenizer.
3. Deduplicate using a stable normalized form (at minimum, Unicode normalization and case-folding); preserve a representative original spelling for display and CSV output.
4. Retain the relevant occurrence context locally for scoring and human review. If a spelling has materially different uses, keep separate word/context examples rather than collapsing distinct meanings into one judgment.
5. Save the extracted evaluation input with its run ID so each prompt version scores identical examples.

## Stage 2 — Score each word and write the CSV

For each unique word, ask Ollaya for the three independent Yes probabilities using the current versioned prompt:

- `is_identifier_percent`
- `is_operationally_significant_percent`
- `is_common_word_percent`

Write `run001.csv` with exactly four columns, in this order:

```csv
word,is_identifier_percent,is_operationally_significant_percent,is_common_word_percent
```

Requirements:

- Store numeric percentages from 0 through 100, consistently rounded (for example, one decimal place).
- Include one row per unique word/context example in the frozen evaluation set.
- Quote CSV values according to standard CSV rules; do not add prose, totals, or review annotations as extra columns.
- Save the exact prompt and run metadata alongside the CSV (for example, `run001.json`) so the result can be reproduced.
- If scoring fails or a signal is unavailable, record the missing result in run metadata and flag the row for review rather than silently treating it as 0% or No.

## Stage 3 — Human error review and error count

Show each word, its three percentages, the threshold-derived predictions, and enough local context to judge the meaning. The user selects every incorrectly classified word and supplies the correct Yes/No value for each incorrect signal. Do not infer a correct label merely from a user's Include/Exclude redaction decision.

Keep `run001.csv` as the unmodified four-column model output. Save human review separately (for example, `run001-review.csv` or a review section in `run001.json`) with the word/example ID, affected signal(s), corrected label(s), and review status.

Report at least:

- **Incorrect word count:** number of unique reviewed words for which one or more of the three predicted labels is wrong. Count each word once in this total.
- **Incorrect signal count:** number of wrong word/signal pairs, with counts split by the three signals.
- False-positive and false-negative counts per signal.
- Reviewed count and unreviewed/unavailable count. Do not count unreviewed examples as correct.
- Error rate over reviewed words: incorrect word count divided by reviewed word count.

The user's corrections become the gold labels for this fixed evaluation set. Preserve corrections across prompt versions and do not let the prompt-revision step change them.

## Stage 4 — Revise the prompt and rerun

Give the prompt-revision model the current prompt, the reviewed misclassifications, their corrected labels, and concise error summaries. Ask it to propose a focused prompt improvement for the specific failure patterns. Do not ask it to alter the gold labels, threshold, or evaluation examples.

1. Save the candidate as the next immutable prompt version (`prompt_version=002`). Record the change rationale and which reviewed errors motivated it.
2. Review the proposed prompt for clear, non-conflicting signal definitions and continued independence of the three labels.
3. Rerun the same frozen evaluation examples with the same Ollaya model/configuration and threshold; write `run002.csv` plus run metadata and review results.
4. Compare error counts/rates against `run001`, both overall and per signal. Keep the earlier run unchanged.
5. If the new prompt does not improve the target error measure or causes a material regression for a signal, record that outcome and revise from the best-performing version rather than assuming the latest prompt is better.

Prompt revision uses the OpenAI Responses API with `PROMPT_REVISER_MODEL` (default `gpt-6-luna`) and `OPENAI_API_KEY` loaded from the repository `.env`. Only reviewed error examples are sent for revision; the full source document and complete word set are not submitted in the revision request. Users should treat the reviewed contexts and labels as data shared with OpenAI.

The default local Ollaya scorer writes every `ollaya list` and scoring subprocess result to `ollaya.jsonl` in the run directory. Each line includes the command, scoring request where applicable, raw stdout/stderr, return code, and any execution error. The log therefore contains each candidate and its bounded text context; it remains local and should be handled as sensitive document data. Injected test/application scorers do not create this log.

## Iteration ledger and success criteria

Maintain an iteration ledger (for example, `prompt_iterations.json`) containing each run ID, prompt version, model/configuration, input-set hash, CSV path, reviewed count, incorrect word count, error rate, per-signal errors, and prompt-change rationale.

The primary objective is to reduce the incorrect word count and error rate on the same reviewed examples. Also require that no signal's error rate regresses beyond an agreed tolerance. Stop when the target error level is reached, successive prompt revisions do not improve results, or the reviewed examples are too few to support a useful conclusion. Report the number of reviewed examples alongside every result; do not claim general accuracy from a small or repeatedly tuned set.

For a more reliable estimate of improvement, keep a separate, user-reviewed holdout set out of prompt revisions. Use the iteration set to tune wording and the holdout only to compare the selected prompt version before adoption.

## Example run artifacts

```text
prompt_v001.txt
run001.csv                 # Exactly four model-output columns
run001.json                # Prompt/model/configuration and input-set metadata
run001-review.csv          # Human corrections and review state
ollaya.jsonl               # Raw stdout/stderr for every local Ollaya invocation
prompt_v002.txt
run002.csv
run002.json
run002-review.csv
prompt_iterations.json    # Version-to-version comparison ledger
```
