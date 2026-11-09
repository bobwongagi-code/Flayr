---
name: flayr
description: Plan product-grounded TikTok commerce content, prepare shooting directions and visual cards, review submitted videos or explicit filming requirements, and prepare personalized content-experience outreach. Use source-linked evidence and human finalization; not automatic grading or outreach sending.
---

# Flayr

Combine product understanding with shoppable-video expression. Keep purchase reasons and whole-video reasoning upstream; give creators concrete visual directions or feedback, not a long internal report.

## Work Map

Choose by the requested outcome. ABC are workflows sharing one foundation, not separate apps or a reason to run the old comparison chain.

| Task | Input | Output and completion boundary | Read |
| --- | --- | --- | --- |
| C: shared product reasoning | Existing product material, buyer questions, reviewed references | Product content brief: supported purchase reasons, distinct directions, shooting conditions, evidence status and limits; content owner confirms reuse | [Planning](references/content-planning.md), [ABC definitions](references/flayr-upgrade-frozen-plan-20261018.md) |
| B: before filming | C or available product evidence, actual assignment and resources | Author direction choices and selected shot card: what to film, prepare and show; no invented personal diagnosis or resources | [Planning](references/content-planning.md), ABC definitions above |
| A: submission feedback | Actual video, material actually sent if any, existing product evidence | Whole-video reasoning, supported priorities and screenshot-linked actions; diagnose as a draft until human finalization | [A review](references/learning-review.md); use its no-teaching branch when no teaching was sent |
| Explicit requirement check | Approved requirements, one existing run, supplied candidate IDs | Bound review packet and separate notes; located does not mean satisfied; no automatic retrieval | [Requirement review](references/requirement-review.md) |
| Zero: content-experience entry | Business-qualified participants and their own footage | Source-supported observation or recut plan plus production handoff; qualification, grouping, production and BD sending remain separate | The current task's explicitly approved Zero plan and entry record; absence of that plan does not authorize selection, production or sending |
| Legacy analysis, only when explicitly requested | Approved videos, route and task authorization | Legacy breakdown/comparison artifacts, with their original verification obligations | [Legacy harness](references/legacy-analysis-workflow.md) |

A does not require a C archive or a reference comparison to start. No explicit requirements means no invented checklist. B and C do not need a fabricated learning-review packet.

## Shared Execution

1. Read the current task's entry record and relevant mode, not every historical log. Restore product purpose, actual sources, authorization and unresolved facts before proposing a local tip.
2. Draft from purchase reasons and whole-video expression, then select concrete shots, actions or edits. Recognize valid alternatives, actual visible results and relevant personal before/after records. Sampling supports “not found,” not “not done.” No automatic grades or fixed number of improvements.
3. Follow [creator communication](references/creator-communication.md) for the actual recipient. Keep internal evidence caveats out of creator prose; do not make beauty filters a default correction or promise sales. Preserve product-use conditions, comparison objects and source times.
4. Run the checks applicable to the changed artifact, below. Finish executor reasoning and evidence review; a script pass is not content approval.
5. Record current artifact/version, check scope and remaining review in the existing task record. Final sending still follows the business owner's authorization. Feedback follows [the improvement loop](references/review-improvement.md); prefer a tool fix or scoped contrast case over another universal rule.

## Checks and Known Limits

| Object | Offline command from repository root | What it establishes |
| --- | --- | --- |
| A learning-review packet, only when teaching exists | `python3 scripts/validate_learning_review.py /path/to/cases.json` | Required fields and readable teaching file only; not required for no-teaching A |
| Structured source bindings, when available | Same command plus `--evidence-index /path/to/evidence-index.json` | Exact source bytes, declared IDs/quotes, video association and bounded times; unresolved facts stay explicit |
| Current author Markdown/text | `python3 scripts/validate_learning_review.py --author-copy /path/to/draft.md` | Limited wording scan; repeat option for affected drafts |
| Final single-file HTML | `python3 scripts/validate_review_delivery.py /path/to/final.html --author-region directions --author-region shoot` | Selected author DOM text, resource/fragment checks, embedded image decoding and final SHA-256; use the actual region IDs |

Bindings and final-file details are in [the improvement/check contract](references/review-improvement.md). Do not rewrite old packets to claim stronger checks retroactively. HTML checks do not run scripts or prove browser layout, screenshot meaning, source truth, local-language naturalness or sharing permission. PDF/audio/video need their actual format's final review; do not pass them off as HTML.

Use [reviewed contrasts](references/review-contrasts.json) as scoped development examples, including counterexamples. Their presence or keyword overlap does not establish semantic success. Old human-reviewed stage lookup remains available via `scripts/lookup_reviewed_cases.py`; it is not the new A/B/C feedback store.

Keep four results separate: automatic check, executor/AI review, human confirmation, not yet verified. Do not collapse them into a global “PASS.” Content changes invalidate the affected file's old verification.

## Scope and Current State

Reuse the existing product foundation and stage responsibilities, not retired automatic gap grades. [Senty migration boundaries](references/migrations/senty-20261107/README.md) preserve public methods versus local-only source assets; do not revive its services or automatic scores.

Task status belongs in its current task entry; durable references describe procedure, not which author is “next.” PAPA-specific business scope and source corrections remain in [its plan](references/papafeel-learning-review-plan-20261016.md) and [retrospective](references/papafeel-pilot-execution-retrospective-20261016.md). Do not reopen settled examples, trigger a pilot, call a model, reprocess video, publish or push merely because a skill describes how.
