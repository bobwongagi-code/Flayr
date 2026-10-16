# Flayr Project Instructions

Scope: this repository only. Read `CLAUDE.md` for existing project conventions. Current explicit user instructions take precedence; historical records do not grant new authorization.

## Creator Communication

Before writing or revising any creator-facing A/B/C deliverable, follow `references/creator-communication.md`. Use concrete, product-grounded suggestions and source-supported claims. Run `scripts/validate_learning_review.py --author-copy <current-draft>` for text handoff (add the material packet for learning review). Integrate human review with the existing content finalization; record final file/version, reviewer/date and disposition in the existing task notes. Recheck affected content after changes, not all unchanged source material. The scan never grants approval or verifies rendered media, factual support or naturalness. Internal analysis and raw source quotes are not creator prose. The legacy production renderer is not migrated by this offline procedure.

Preserve the latest business corrections: beauty filters and personal shooting style are not default diagnosis or revision targets; keep internal evidence caveats out of creator prose. Recognize real before/after and multi-day personal-use records with visible results as valid effect evidence for content review, without imposing clinical causal-proof standards. Judge what the material actually shows, not a same-day/multi-day rule; never invent a fixed seven-day outcome. Current source-linked facts and user confirmations remain the basis. These are review principles, not an automatic production scoring change.

## Direction A Entry

For learning-submission review (a reference breakdown or storyboard and submitted videos), follow `references/learning-review.md` before writing recommendations. Use the existing product foundation and whole-video reasoning; do not default to the legacy compare/improve pipeline. For explicit requirement checks, use the separate requirement-review mode in `SKILL.md`. The learning-review checklist command is `python3 scripts/validate_learning_review.py <cases.json>`; it checks material completeness only, never quality or delivery approval.

Keep confirmed business scope separate from unresolved facts and delivery approval. When scope changes, synchronize the active packet, short review table and reply status notes; do not leave obsolete "waiting for scope" blockers in the reader's entry point. The shared review procedure is `references/learning-review.md`; historical case documents are evidence, not competing procedure definitions.

Before handing off learning-review drafts, complete that procedure's pre-handoff execution review in the existing batch record: priority rationale, counterevidence checked, sentence-to-media support, and known-case corrections. This is the executor's responsibility, not a new user form. A completeness-check pass never substitutes for semantic review or delivery approval.

## PAPA FEEL Learning Review

Before continuing this task, read:

- `references/papafeel-learning-review-plan-20261016.md` for approved scope and current artifact links.
- `references/papafeel-pilot-execution-retrospective-20261016.md` for execution failures, corrections, and unresolved evidence.

Preserve these confirmed decisions:

- The teaching video explains reference practices; the storyboard is a supporting framework, not a mandatory shot checklist.
- Start with the product and buyer's question, then the reference's purpose, the teaching explanation, and the submission's actual information contribution. Framing and editing are means, not the default analysis target.
- Apply the existing product foundation, not just its selling-point list: connect `usage_context`, consumer outcome, observable signals and proof conditions to the teaching and submission. Manufacturer claims are reference inputs, not the local creative answer or verified effects. The user has explicitly required actual use and before/after for this PAPA FEEL learning task; do not ask again whether these belong. This does not amend global S3/S4 rules or authorize invented outcomes, instant-effect claims or a fixed recording duration.
- Before selecting a local tip, assess the whole video's product persuasion: the need raised, product explanation, use/results and supporting trust, and purchase invitation. Reuse the current stage responsibilities, not gap grades or a mandatory six-section checklist. Read `references/papafeel-capability-reuse-20261016.md` for verified source mapping and superseded rules. A safe, visible detail must not displace a supported major content gap.
- Explain where a teaching point appears, how the expression serves its purpose, and what is worth improving. Recognize valid alternatives; do not force an improvement for every submission or introduce scores and gap grades.
- Do not mistake sampled absence for missing content, actions for product effects, or marketing claims for verified efficacy. Check necessary speech and context before diagnosing an information gap.
- For this pilot, keep the source-labelled product foundation, product question, teaching rationale, evidence and all three `whole_video` fields in `product-review/cases.json`. Its local `validate_cases.py` delegates to the repository learning-review check. This checks presence, not truth. Follow the plan's absence standard: sampling supports "not found"; confirmed absence needs full relevant review or explicit scoped human verification. Brief valid evidence must not be erased by a minimum-duration rule.
- Form a supported judgment before asking the user to confirm it. Ask about specific unresolved facts or business choices, not for the user to reconstruct the analysis.
- Keep the rationale in internal analysis; author suggestions must specify an executable shot, action, or edit, not just "explain clearly" or "focus on the main point." Use the plan's compact review entry first; expand evidence only when needed. Concrete wording does not waive evidence or product-use checks.

Keep current status, historical failures, and authorization separate. The product-oriented v2 drafts are not approved deliveries or evidence of accuracy/value gains. Read linked records for the latest state rather than treating this entry as a status ledger.

When resuming from the parent workspace, explicitly read this file before Flayr work; do not assume a nested instruction file was automatically loaded. Keep detailed case evidence in the linked documents, not in this entry.
