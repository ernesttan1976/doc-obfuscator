# Document Text Obfuscation App

## Product Requirements Document

Version 1.1 · 4 October 2026

## 1. Product purpose

Build a local folder app that helps a user replace sensitive text in documents before sending them to an external LLM, then restore those terms in an Office document returned by the agent. The user decides what is sensitive in the context of each document. The app suggests similar terms, groups them in a document-specific graph, and previews proposed replacements through a 1–10 slider and direct click-to-include or click-to-exclude actions.

The first release supports text replacement in Word `.docx`, PowerPoint `.pptx`, and plain-text files such as `.txt`, `.md`, and `.csv`. It focuses on text. It does not aim to inspect, sanitise, or explain every internal Office feature. Restoration is offered only for agent-returned Office documents. A returned plain-text response remains obfuscated.

The app should feel like a private cloud folder, with projects, files, versions and status. For the initial release, files and their term graphs remain on the user's computer. The app prepares an obfuscated file for manual upload to any external agent; it does not need an LLM account or send documents to a service itself.

## 2. Problem and intended outcome

People want LLM help with reports, presentations and text files, but a name, unit, project term or combination of facts may need to be hidden first. A fixed universal list cannot decide what is sensitive in every document. Manually replacing terms misses variants and repetitions, while reconstructing them in an edited deliverable is tedious.

The app gives the user a fast way to decide what to mask, inspect the effect in context, exchange a usable file with an agent, and restore known placeholders in the agent's returned Word or PowerPoint file.

This is a disclosure-reduction tool. It does not certify that a document is anonymous, that every sensitive fact was found, or that the user is authorised to share a particular document.

## 3. Core workflow

1. Create a project and import a `.docx`, `.pptx`, or supported text file.
2. The app extracts editable text and proposes terms and similar-term groups. It creates a private graph for this document version.
3. Move the slider from 1 to 10. The preview highlights terms that would be replaced at the selected level.
4. Click a word or select a phrase to include or exclude it. The decision applies to its confirmed similar-term group. Review suggested group members before accepting them.
5. Inspect the resulting obfuscated preview. Approve and export that version when it is ready for external use.
6. Send the exported file to an LLM agent yourself. Ask it to preserve placeholders and return a `.docx` or `.pptx` file.
7. Import the returned Office file into the same project. The app uses that document's local graph and replacement map to restore exact, intact placeholders.
8. Review unresolved placeholders and save a new restored file. Chat replies and returned `.txt`, `.md`, or `.csv` files do not have an automatic restoration action.

Example, using fictional content:

| Stage | Text |
| --- | --- |
| Original | Alex Tan briefs Project Cedar. |
| Obfuscated | `[[T_001]]` briefs `[[T_002]]`. |
| Agent's PowerPoint | Project owner: `[[T_001]]` / Project: `[[T_002]]` |
| Restored PowerPoint | Project owner: Alex Tan / Project: Project Cedar |

The replacement map and graph are never included in the file sent to the agent.

## 4. Slider and preview

The slider controls how broadly the app proposes terms for obfuscation in the current document. It is a review aid, not an official sensitivity rating or a guarantee. A suggested starting point is level 5. Profiles can assign candidate groups to levels, but the user can change any decision for the current document.

- Level 1 shows a narrow set of high-confidence matches and terms the user has explicitly prioritised.
- Higher levels add more candidate groups, including less certain similar terms and context-linked terms.
- Level 10 shows all candidates the app can identify under the current profile. It cannot show terms detection did not find.
- Moving the slider updates the preview and match counts. A higher level does not undo a user's manual Exclude decision.
- Manual Include and Exclude decisions stay pinned as the slider moves. Resetting them requires an explicit, undoable action.
- Changing the document, graph, profile or slider invalidates approval for any earlier export derived from those settings.

The preview displays the page or slide text with proposed replacements highlighted. A text view supports dense content. Users can navigate through all pages or slides and see counts, graph groups, unresolved coverage and what changed at the selected slider value. Show labels as well as colour to distinguish automatic suggestions from manual choices.

Exact slider tiers and similarity thresholds are configurable profile choices to validate with users. The levels must not imply that the same kind of term is always sensitive. A date may be included in one document and excluded in another.

## 5. Click-to-edit and similar-term graph

Clicking a word or selected phrase opens **Include in obfuscation** and **Exclude from obfuscation**. The app applies the decision throughout the current document to matching terms in that word's group, then refreshes the preview. Show how many occurrences will change and provide Undo.

Similarity detection proposes groups from spelling variants, abbreviations, aliases and contextual similarity. The app must show why terms were grouped and let the user accept, split, merge or remove group members. It must not silently treat a broad semantic association as proof that two different terms are interchangeable. A manual Exclude in one group member cannot be overridden by an automatic suggestion.

Each document version has an associated local graph object. The graph records term nodes, similar-term and alias edges, confidence, occurrences, the user's include/exclude decision, slider tier and the placeholder mapping needed for restoration. The graph is encrypted and stored beside the document in the private project workspace. It is not embedded in or exported with the obfuscated Office or text file.

Illustrative graph shape:

```text
Document version
  ├─ term node: "Project Cedar" ── alias edge ── "P. Cedar"
  │       └─ decision: Include → token [[T_002]]
  ├─ term node: "Alex Tan" ── alias edge ── "A. Tan"
  │       └─ decision: Include → token [[T_001]]
  └─ occurrence edges: source text locations for each matched term
```

Graph decisions are scoped to a document by default. The user may apply a confirmed group across selected files in one project. Each document retains its own occurrences and decisions. A new project starts with a separate graph and token namespace unless the user deliberately reuses a profile or dictionary.

### Similarity approach

Use RapidFuzz for spelling and formatting variants. Use the local encoder `sentence-transformers/all-MiniLM-L6-v2` to suggest contextually similar terms by comparing each term with its surrounding text. Store similarity scores as proposed graph edges; users must confirm a group before its members are obfuscated together. Term similarity can link related but distinct entities, so scores never trigger replacement on their own.

## 6. Text file behaviour

For `.docx` and `.pptx`, the product requirement is text replacement and restoration in supported editable text. Preserve surrounding text and formatting where possible. Keep originals unchanged and save every processed result as a new version. The product should not make Office file internals a user-facing concern.

For `.txt`, `.md`, and similar text files, replace terms in place while preserving line breaks and ordinary text structure. For `.csv`, process cell text while preserving rows, columns, quoting and delimiters. Encoding or malformed text that cannot be safely round-tripped must be reported before export.

The app is not a general Office cleanup or document-forensics utility. The first release does not promise detection or removal of comments, revision history, file metadata, embedded objects, text inside images, speaker notes, hidden content, formulas, macros or other non-body text. It does not process `.xlsx` workbooks as workbooks in the first release; CSV is the supported plain-text spreadsheet exchange format. The import view must state that the feature covers editable text so users do not mistake it for complete file sanitisation.

## 7. Replacement and restoration rules

- Generate opaque, random placeholders that do not disclose the original term or its category. Detect pre-existing placeholder-like text to avoid collisions.
- Use exact, known placeholder matches for restoration. Do not guess, fuzzy-match altered tokens, or infer a missing token from context.
- Restore placeholders only after the user imports a `.docx` or `.pptx` and associates it with the correct local document graph.
- Support placeholders that the agent moves or repeats. Report modified, unknown, or foreign-project tokens for review.
- Preserve the agent's returned document structure and replace only remaining valid placeholders. Do not reinsert passages the agent removed.
- Do not restore a plain-text reply or automatically de-obfuscate chat text, clipboard content, prompts, previews or non-Office attachments.
- A restored file is labelled complete only when all recognised placeholders are handled and no unresolved tokens remain. The restoration report does not assert factual accuracy.

The user must be able to preview the output before export or restoration. Errors must identify the document, term or operation that needs attention. Never overwrite the original.

## 8. Local storage and privacy

The initial release processes files locally and has no hosted LLM connection or background document upload. Keep originals, graphs, mappings and restored files in a private project workspace protected by operating-system account access. Encrypt stored graphs and mappings at rest. Ollaya's local request and response are written to one backend log line per candidate as an explicit diagnostic exception; do not send these logs externally.

Only the user-approved obfuscated file leaves the workspace when the user exports it. After export, the app cannot control where the file is sent or retained. Loss of the local graph or map prevents automatic restoration, so provide an encrypted local backup or a clear warning before deletion. The graph contains sensitive term relationships and is protected as carefully as the original document.

## 9. MVP requirements and acceptance

| ID | Requirement | Priority |
| --- | --- | --- |
| FR01 | Local project folder, import, versions and processing status. | Must |
| FR02 | Text extraction and replacement in `.docx` and `.pptx`. | Must |
| FR03 | Plain-text support for `.txt`, `.md`, `.csv` and documented additional text extensions. | Must |
| FR04 | Per-document similarity graph with visible groups, confidence and source occurrences, suggested by RapidFuzz and local MiniLM embeddings. | Must |
| FR05 | 1–10 slider with live highlighted preview and match counts. | Must |
| FR06 | Click-to-include/exclude with group propagation, manual overrides, split/merge and Undo. | Must |
| FR07 | Opaque placeholders, private local mapping and exact restoration in returned `.docx` or `.pptx`. | Must |
| FR08 | Unknown token report and explicit project association on returned Office file. | Must |
| FR09 | No automatic restoration of plain text, chat responses or copied text. | Must |
| FR10 | User-facing scope statement that limits coverage to editable text. | Must |

Acceptance tests use synthetic documents with known terms and do not require sensitive material:

- Moving the slider changes candidate highlights predictably and does not override pinned manual decisions.
- Clicking Include or Exclude updates all confirmed group occurrences in scope; unrelated similarly named terms remain unchanged.
- Users can review, split and merge proposed groups before export.
- Similarity suggestions appear as unconfirmed graph edges; no group is obfuscated together until the user confirms it.
- Word and PowerPoint text is replaced and restored while retaining surrounding content and formatting in the supported test set.
- Markdown and CSV replacements preserve line structure, fields, quoting and delimiters in the supported test set.
- A returned `.docx` or `.pptx` restores only intact placeholders from the selected document graph. Changed or foreign tokens remain unresolved.
- Plain-text agent returns have no restoration action.
- Exported files contain no graph or mapping sidecar. Local diagnostics contain no raw original terms.
- Originals are unchanged and every obfuscated and restored file is a new version.

## 10. Delivery and decisions to resolve

Prototype the graph review, slider and click behaviour with synthetic text first. Then implement the `.docx` and `.pptx` text round-trip and the plain-text formats. Pilot with non-sensitive documents and revise similarity groups and slider tiers from user feedback.

Resolve these choices before implementation:

1. Which operating system should the first release target?
2. Is `.xlsx` text-cell replacement needed, or is `.csv` sufficient for spreadsheet workflows?
3. Should term similarity be detected only with local algorithms, or may users enable a local language model?
4. Should a confirmed term group apply across every project file by default, or only to files the user selects?
5. Which plain-text extensions and encodings should ship in the first release?
6. What file sizes and preview response times should the first release target?
7. What lock timeout and encrypted backup method should protect the local graph and mapping?
8. Which external LLM services are permitted for the intended documents?

## Reference

Microsoft Support, [Remove hidden data and personal information by inspecting documents, presentations, or workbooks](https://support.microsoft.com/en-us/office/collab-files/remove-hidden-data-and-personal-information-by-inspecting-documents-presentations-or-workbooks), accessed 4 October 2026. The app's editable-text-only scope is deliberately narrower than the categories described in this reference.
