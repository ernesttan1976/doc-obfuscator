# Ollaya annotation, calibration, and Von fine-tuning plan

**Status:** Research-backed plan; implementation and training have not started.
**Target application:** Blot local document obfuscation.
**Base runtime/model:** Ollaya with the pinned `von:1.1` model.
**Research checked:** 2026-10-06.

## Executive recommendation

Collect explicit user-reviewed labels for each candidate word in a visible, short context, then save them in Blot's encrypted project state. The label set is:

1. `is_identifier` — boolean
2. `is_operationally_significant` — boolean
3. `is_common_word` — boolean

Keep an unanswered/incomplete state until the user confirms all three values. A decision to Include or Exclude a word is a separate user action and must not be silently converted into these three semantic labels.

Start with **evaluation and calibration**, not weight updates. Calibration can make probability estimates more honest for Blot's distribution but cannot change the chosen Yes/No decisions. If the goal is to change those decisions, train Von on the reviewed examples using its native option-marker task format, evaluate against a held-out set, export a compatible checkpoint, and serve it under a separately versioned Ollaya model name. Do not replace the base model in place.

There is an important Ollaya deployment gate: its documented `/api/push` is reserved and returns `501 NOT_IMPLEMENTED`. `ollaya create`/Modelfiles are useful for model configuration and embedded questions but are not a way to fine-tune Von's weights. A trained checkpoint therefore needs a validated custom/private Ollaya registry or an Ollaya change that loads local custom weights. Prove this packaging path before investing in a full training run.

## Findings

### Current Blot behavior

- `backend/app/ollaya_scoring.py` already asks the local `von:1.1` model all three questions and validates typed Yes/No probabilities.
- `data_flow.md` describes the three outputs, streaming candidate scoring, caching and encrypted graph persistence.
- Candidate analysis and explicit Include/Exclude decisions are saved in the encrypted, document-version-scoped project graph. The scorer's bounded context is transient for ordinary scoring and is not currently saved as annotation data.
- The existing UI exposes model signals for review, but there is not yet a dedicated interaction that asks a person to confirm each of the three semantic labels and saves the labels as training examples.

The implementation should build on the current local scoring stream and encrypted graph instead of adding a remote labeling service or an unencrypted analytics database.

### Ollaya and Von constraints

- Ollaya runs local decision models and supports typed `choice`, `score`, and `noul` outputs with option probabilities. Its HTTP API has `/api/decide` and TypeSafe-compatible `/v1/systemone`; Blot currently invokes the local CLI.
- Ollaya's repository separates its Rust serving/runtime and registry from model conversion code in `convert/`. Model artifacts are packaged as a graph, weight/tokenizer layers and decision/calibration metadata; registry manifests pin layer digests.
- The Ollaya Von family documentation describes `von:1.1` as an English ModernBERT-large encoder (~395M parameters) with an option-marker scoring head, using a Von-specific sequence layout and input-conditioned temperature calibration. It is not interchangeable with Laya exports.
- The referenced Von 1.1 checkpoint is pinned to `wfzyx/von` revision `d8bb5e0745d8ee1fb65d536d6d4892d54d5a93fd` and `von-sdk==1.1.1` in the Ollaya family notes. Do not train from mutable `main`, `latest`, or a later Von checkpoint by accident. Von's current model card describes newer releases, so pin the exact base weights and code before any experiment.
- The 1.1 `option_marker.pt` checkpoint is a 1.58 GB PyTorch zip/pickle, not a safetensors checkpoint. Load only the trusted, revision-pinned upstream artifact in an isolated training environment. Never load user-supplied checkpoints as part of Blot's app workflow.
- Ollaya reports a `von:1.1` model size of about 1.58 GB and 8,192-token context. The app's own word/context requests are tiny; training resource requirements still need measurement on the actual training hardware.
- The Von model card describes a native `von calibrate labels.jsonl` path with frozen weights. Verify the exact 1.1 CLI syntax/schema against the pinned Von SDK before using it. A fitted Von calibration file must also be mapped to the calibration format supported by the chosen Ollaya registry/runtime.

### Calibration versus fine-tuning

| Operation | Changes model weights? | Can change Yes/No decisions? | What it can improve |
| --- | --- | --- | --- |
| Temperature/calibration fit | No | No (temperature is monotonic) | Reliability of reported probabilities and confidence thresholds |
| Fine-tuning Von | Yes | Yes | Accuracy on the target task, if held-out results improve |
| Changing question wording | No | Possibly | Prompt/task framing; must be separately evaluated and versioned |

First determine whether the actual defect is wrong labels, overconfident probabilities, inconsistent treatment of word/context, or poor question phrasing. Use a frozen baseline and held-out labels to isolate these before training.

## Annotation UX and semantics

### What the person sees and answers

For each extracted unique word, show:

- The word and one representative occurrence context (the same bounded, at-most-192-character window currently sent for scoring).
- Ollaya's three current answers and probabilities, clearly marked as model suggestions.
- Three explicit Yes/No controls for the human labels. Require confirmation of all three before marking the annotation complete. Permit “skip for now” without inferring negative labels.
- A way to inspect a different occurrence when the same word has materially different meanings in the document. Labels belong to the word **in the shown context**, not globally to a spelling.
- An edit/review path for labels already saved; record updates as a new annotation revision so training provenance is auditable.

Definitions must be short, consistent and versioned with the question template:

- **Identifier:** in this context, does the word/name identify a person, organization, project, location, system, codeword, unit or other named entity?
- **Operationally significant:** in this context, does it identify or distinguish an operational activity, capability, vulnerability, plan or resource?
- **Common word:** is the candidate a common English word? Ignore capitalization alone; judge the word itself, independently of whether it is used as an entity in this context.

Keep `is_common_word` independent from the other two labels. A word can legitimately be both common and an identifier/operational term in context. Do not force mutually exclusive labels.

### Stored annotation record

Persist annotation records inside the existing AES-GCM encrypted private project graph (or a separately encrypted sidecar with the same key lifecycle), scoped to the source document version. Example logical record:

```json
{
  "annotationId": "stable-random-id",
  "schemaVersion": "word-labels-v1",
  "questionVersion": "blot-word-signals-v1",
  "term": "Falcon",
  "context": "The Falcon team will stage the deployment on Friday.",
  "labels": {
    "is_identifier": true,
    "is_operationally_significant": true,
    "is_common_word": true
  },
  "status": "confirmed",
  "source": "human_confirmed",
  "sourceDocumentVersionId": "version-id",
  "createdAt": "UTC timestamp",
  "updatedAt": "UTC timestamp"
}
```

Use actual JSON booleans. Incomplete records may have `null` labels and `status: "incomplete"`, but only fully confirmed records enter a training/evaluation export. Keep the word, context and document/version provenance encrypted at rest. Context is necessary because two of the labels are context-sensitive; do not broaden it to full paragraphs or documents.

Store annotations separately from Ollaya's cached inference results and candidate decisions. Keep model output, human labels, and user Include/Exclude decisions as distinct data. Deleting a project or its encryption key must also remove/invalidate its annotation material according to the existing project deletion/recovery semantics.

### Training-data export and privacy

- No annotation, word, context, document ID, model response or training artifact leaves the machine automatically. Do not upload training data to Ollaya, Hugging Face, a telemetry endpoint, or an external notebook.
- Add an explicit user action to export a training set. Export only confirmed records from selected project(s), show record counts and label balance first, and write only to a user-selected local training workspace.
- Strip project names, original filenames, file paths and stable project/document IDs from the model examples. Keep a local-only manifest mapping the export to provenance if needed.
- Training workspace files containing plaintext terms/context are sensitive. Restrict file permissions, avoid logs/backups that silently expose them, document cleanup, and keep transient plaintext export files in a private temporary directory where practical. Do not claim exported JSONL is encrypted unless it actually is.
- Support deleting an annotation and regenerating an export. Preserve the source project graph as the system of record; generated train/validation files and model checkpoints are reproducible derived artifacts.

## Dataset construction

### Example shape

The model input should reproduce Blot's inference shape: a small state with a candidate and its bounded context, plus three described binary `choice` questions. Use the actual prompts/descriptions shipped by the app, versioned, not a separate informal training prompt.

Conceptual source row:

```json
{
  "state": {
    "candidate": "Falcon",
    "context": "The Falcon team will stage the deployment on Friday."
  },
  "labels": {
    "is_identifier": true,
    "is_operationally_significant": true,
    "is_common_word": true
  }
}
```

Convert labels into the exact Von/Ollaya typed-question example format supported by the pinned trainer: each label becomes the gold option (`Yes` or `No`) for its corresponding two-option question. Confirm the training serializer on a tiny fixture before preparing the full corpus. Keep exact question IDs, instructions, option descriptions and ordering aligned with Blot inference.

### Data quality and splits

1. Validate types, required context/term, label completeness, schema/question versions, duplicate records and conflicting annotations. Route conflicts for human adjudication; never resolve them by last-write-wins in the training set.
2. Normalize only for deduplication and split grouping; preserve the original case/text in the inference state. Do not merge different contexts solely because their word spelling matches.
3. Split by source document and near-duplicate text so snippets from one document cannot land in train and test. Add a separate lexical generalization slice that holds out normalized words where dataset size permits. This reveals memorization of frequent spellings.
4. Keep a frozen test set that is not used for prompt selection, calibration, checkpoint selection or training. Version and checksum each split.
5. Track coverage and balance independently for each binary task. Include hard contrasts: same word with different contextual roles, ordinary/common words used as project/call signs, rare names, operational verbs/resources, ambiguous abbreviations, malformed/noisy extracted words, and negative examples that are neither identifiers nor operationally significant.
6. Do not treat missing user feedback or a skipped candidate as a No. Do not derive any of the three semantic labels from Include/Exclude actions.
7. Begin with a small pilot to test annotation definitions and inter-reviewer consistency. Before weight training, set minimum per-label positive/negative counts and statistical acceptance targets from the observed label distribution; a few dozen corrections are not sufficient evidence for reliable model fine-tuning.

## Training and evaluation workflow

### Stage A — Baseline measurement

- Pin and record Ollaya version, the exact Von 1.1 registry manifest digest, upstream Von revision, `von-sdk` version, Blot question template, runtime precision and machine details.
- Collect a human-confirmed gold set; run the frozen model on the same examples and save raw predictions/probabilities with the test data version.
- Report per-task confusion matrix, precision, recall, macro-F1/balanced accuracy, PR-AUC where meaningful, log loss, Brier score and expected calibration error (ECE). Include class prevalence and sample counts; aggregate accuracy alone hides weak minority labels.
- Inspect error slices for context length, common-word status, capitalization, entity type, source format and ambiguity. Keep the baseline immutable for paired comparisons.

### Stage B — Calibration first

- Fit calibration parameters using a dedicated calibration split, not the final test split. Start with the pinned Von `calibrate` utility if its 1.1 interface accepts the app's examples and three signals.
- If the utility calibrates at model/question type rather than per Blot signal, verify whether it can distinguish the three question IDs. Do not assume one global temperature fixes task-specific calibration.
- Re-run the frozen test set and compare Brier/ECE/log loss and reliability diagrams per label. Confirm decision labels remain unchanged if this is temperature-only.
- If calibration materially fixes overconfidence but label accuracy is already acceptable, ship the calibrated model/config as the first iteration and continue collecting reviewed labels before weight training.

### Stage C — Fine-tune Von only after data and deployment gates

- Use the pinned Von 1.1 training implementation/revision and exact Von option-marker serialization. Preserve the architecture's option-marker head, independent label examples as supported by that trainer, and a broad-data replay/regularization strategy where available to reduce catastrophic forgetting.
- Prefer a small, reproducible domain adaptation run with a held-out validation set, deterministic seeds, recorded hyperparameters and an untouched test set. Test a frozen-backbone/head-only or parameter-efficient experiment only if the pinned training code supports it and the Ollaya export/runtime can preserve that format; otherwise follow the upstream-supported full/partial fine-tuning path.
- Train the three questions as separate binary judgments from the same state. Maintain label independence; do not turn the three labels into a single 8-class mutually exclusive target.
- Compare the tuned checkpoint to frozen Von 1.1 and to the calibrated-only model using paired per-example metrics and bootstrap confidence intervals. Select a checkpoint on validation data only.
- Verify no unacceptable regression on the broad Von/Ollaya capability/parity fixtures. Task gains on Blot labels do not imply overall Von improvement; name and document this as a Blot-specialized model.
- If the held-out data are too small or the gains are statistically inconclusive, do not ship new weights. Continue collecting labels or ship calibration-only if that improves reliability.

### Evaluation/release gates

A model is not ready to deploy until all of these are satisfied:

- The held-out evaluation shows a pre-agreed meaningful improvement in the target metric(s) for each signal of interest, with uncertainty intervals reported. No unexplained severe loss in precision/recall for any signal.
- Calibration metrics improve or remain within a pre-agreed tolerance; report decision quality and probability quality separately.
- Same-word/different-context examples behave appropriately; lexical holdout performance is disclosed.
- No test rows, related document snippets, or test-set labels were used for fine-tuning or checkpoint selection.
- Von reference inference, Ollaya converted graph, and Ollaya local runtime pass the pinned parity suite on golden cases. Include probabilities/logits within defined tolerances and exactly matching expected answers.
- Resource/latency tests pass on target macOS and Windows machines. Von is ~395M parameters and its source weights are ~1.58 GB; measure actual peak RAM/VRAM and CPU latency rather than extrapolating.
- Artifact license, attribution, upstream revision, data provenance, training configuration, checksums and model card are present.

## Packaging and local deployment

1. **Prove the route with a no-op/custom calibration artifact first.** Establish how a custom Von-compatible graph, weight layer, tokenizer, decision metadata and calibration map can be referenced by a private registry accessible to the installed Ollaya runtime. The Ollaya Von 1.1 family notes document conversion and parity commands and the expected graph contract.
2. **Do not assume Modelfile fine-tuning.** `ollaya create` can construct/configure an Ollaya model, but it does not train the Von encoder/head. The Ollaya HTTP API currently reserves model push (`/api/push` returns 501), so determine whether an Ollaya local registry can serve an app-specific model name without a public upload. If not, scope an Ollaya fork/feature request for trusted local custom artifacts before doing training.
3. Build an immutable release such as `blot-von:0.1.0` (name illustrative), with a new digest and manifest. Keep the base `von:1.1` installable for rollback and evaluation. Never overwrite the upstream tag or silently track `latest`.
4. Ensure Ollaya's calibration loader consumes the fitted calibration metadata and that the graph is appropriate for the trained checkpoint. If the training run changes only weights but retains the Von 1.1 architecture/layout, rerun Ollaya's Von export/parity workflow; changes to architecture/layout require a corresponding Ollaya runtime/converter change.
5. Change Blot's scorer to accept a configured model tag and expected response tag while preserving `von:1.1` as fallback. Include model digest, question/scoring version, calibration artifact version and training-set version in result metadata. The score cache key must include all of these so model updates cannot reuse stale results.
6. Install/select the custom model only through an explicit local user action. Do not download weights automatically. Verify integrity, keep the source model available for rollback, and keep candidate analysis non-blocking if the custom runtime is unavailable.
7. Run smoke and golden tests using synthetic examples, then compare selected human labels inside Blot before making the custom model the default. Provide a setting to return to `von:1.1`.

## Proposed implementation phases

### Phase 0 — Freeze specifications and prove custom-model support

- Pin and locally verify the exact `von:1.1` checkpoint/registry manifest and current Ollaya/Von versions.
- Confirm the pinned Von 1.1 training, calibration, export and parity APIs; verify the actual label JSONL schema.
- Prototype installing/serving a named local custom Von artifact without touching Blot production state. Decide private registry versus Ollaya fork based on a working test.
- Finalize label definitions, occurrence/context presentation, annotation schema, deletion semantics and evaluation metrics.

**Gate:** a tiny synthetic dataset can be serialized, calibrated/trained by the pinned toolchain, packaged under a separate model name, and answered by the installed Ollaya runtime with correct parity. If not, resolve packaging/trainer changes before collecting a large dataset.

### Phase 1 — Annotation persistence and review UI

- Add the explicit three-label confirmation UI and incomplete/skipped/edit states to streamed candidate review.
- Add encrypted version-scoped annotation persistence separate from scoring signals and Include/Exclude decisions.
- Preserve context only for confirmed/edited training annotations; keep it bounded and encrypted. Don't persist a training context merely because inference ran.
- Add clear local-data and export disclosure; make label edits auditable/versioned.

**Gate:** tests show confirmed labels survive restart and project backup/restore, incomplete labels are excluded from exports, re-analysis retains user annotations, and deleting project state removes them.

### Phase 2 — Dataset export and quality review

- Implement explicit, local-only export of confirmed examples and manifests with hashes, schema versions, aggregate label balance and dedup/conflict reports.
- Implement grouped document/near-duplicate split generation and a lexical holdout evaluation slice.
- Build an annotation QA view for conflicts, low-context examples, and per-label balance. Review definitions with a pilot before broad labeling.

**Gate:** reproducible exports are built from encrypted source records, contain no paths/project identifiers, and are split without document leakage.

### Phase 3 — Baseline, calibration and fine-tune experiment

- Benchmark frozen `von:1.1` on the gold set; evaluate the exact three Blot tasks separately.
- Fit/measure calibration on the reserved calibration split.
- Fine-tune only if the data sufficiency and deployment gates pass; log code/data/base-model hashes and hyperparameters.
- Evaluate the tuned model against both frozen and calibrated-only baselines on locked validation/test sets.

**Gate:** measurable held-out benefit for target tasks, acceptable calibration and general capability regression, passing parity/resource checks.

### Phase 4 — Versioned model release and Blot integration

- Package a separate custom model tag and publish/serve it through the proven private/local Ollaya route.
- Add configured model selection, integrity/version metadata, cache invalidation and rollback to Blot.
- Run local offline integration, persistence, fallback, parity and target-platform resource tests.

**Gate:** explicit install/selection works offline after acquisition, base `von:1.1` remains available, and a failed custom model cannot block document review/export.

## Operational, privacy and product invariants

- Blot stays local-first: no network transmission of document text, candidate words, contexts, labels, predictions or training data.
- Inference suggestions are not human labels; model predictions must never auto-confirm training annotations.
- Explicit Include/Exclude actions govern redaction behavior only. They do not imply `is_identifier`, `is_operationally_significant` or `is_common_word` labels.
- Keep annotations document/version scoped in the project source data. Training exports and trained models are explicitly generated derived artifacts, not silent cross-document personalization.
- Keep a human-reviewable way to correct or delete labels and to identify the model/version that produced past results.
- Model probabilities remain reported estimates, not guarantees. User-facing wording must distinguish classification quality from calibrated probabilities.

## References

- [Ollaya repository](https://github.com/ollaya-dev/ollaya) — local runtime, registry, converter and Rust tests.
- [Ollaya HTTP API contract](https://raw.githubusercontent.com/ollaya-dev/ollaya/main/docs/api.md) — typed decisions, model create/push behavior, local API.
- [Ollaya Von family notes](https://raw.githubusercontent.com/ollaya-dev/ollaya/main/docs/families/von.md) — Von 1.1 layout, pinned source revision, artifact layers, conversion/parity commands and known constraints.
- [Ollaya Von 1.1 manifest](https://raw.githubusercontent.com/ollaya-dev/ollaya/main/registry/v2/library/von/manifests/1.1) — pinned registry layer digests and upstream asset URLs.
- [Von model card](https://huggingface.co/wfzyx/von) — architecture, calibration, training and task limitations. The model card advances over time; use the immutable 1.1 revision above for this plan's base.
- Blot [`data_flow.md`](./data_flow.md), [`ollaya_redaction_scoring.md`](./ollaya_redaction_scoring.md), [`backend/app/ollaya_scoring.py`](./backend/app/ollaya_scoring.py), and [`backend/app/projects.py`](./backend/app/projects.py).

## Decisions to confirm before implementation

1. Should training labels always include the bounded context shown to the annotator (recommended, because identifier and operational meaning depend on context), or should the app label isolated spellings only?
2. Is the intended trained model a private, per-user Blot specialization, or a distributable shared model? This changes consent, data aggregation, artifact hosting and governance; the default plan assumes local/private specialization and no user-data pooling.
3. What hardware is available for full/partial Von training, and what latency/memory target must the deployed model meet on both supported OSes?
4. Which improvement is most important per signal—precision, recall or balanced error cost? Set numerical release thresholds from a reviewed pilot before examining the final test results.
