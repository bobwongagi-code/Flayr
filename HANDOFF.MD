# Flayr Handoff

## Dependency Security Pins (2026-11-09)

- Pillow is pinned to `13.0.0` and Black to `26.10.1`. Earlier notes accepting a Pillow/Black audit blocker describe historical pushes, not permission to ignore future audit failures.
- Keep the lock, dependency documentation, Unreleased maintenance note and project contract tests synchronized. The synthetic contact-sheet test exercises real PNG/JPEG decoding, resizing, text and JPEG output without modifying its inputs or processing business videos.
- Dependency upgrades require an isolated install, `pip check`, dependency audit, full offline tests and the complete quality-gate chain. Do not suppress advisory IDs or weaken tests/coverage to obtain green CI. No scoring, GT, model configuration or production source is changed by this maintenance batch.

## Offline Skill Improvement (2026-11-09)

- `SKILL.md` routes product briefs, shooting cards, submission feedback, explicit requirements and content-experience entry tasks without defaulting to the old comparison chain. Legacy commands remain in `references/legacy-analysis-workflow.md`; historical JSON imports are audit-only/degraded.
- `references/review-improvement.md` defines source bindings, final-file checks and the feedback loop. `references/review-contrasts.json` holds scoped development contrasts with sources and counterexamples, not GT or an independent semantic benchmark.
- `validate_learning_review.py --evidence-index` checks declared IDs, source bytes and ranges; old packets retain their limited completeness check. `validate_review_delivery.py` checks selected author DOM text, embedded image decoding, resources, fragments, local paths and final hashes without executing scripts or granting sending approval.
- Targeted tests and adversarial regressions cover parsing, malformed input, stale bindings, hidden wording and truncated images. Automatic checks, executor review, human confirmation and unverified items remain separate. Production source, scoring, GT, model routes and dependency pins are unchanged; the semantic freeze verifier passes without refreezing.
- Keep local task artifacts, videos, private source paths and unrelated unfinished plans out of public commits. The user authorized this scoped push while retaining the existing Pillow/Black dependency-audit CI blocker; no audit waiver or dependency change belongs in this batch.

## Senty Content Migration (2026-11-07)

- Current product-to-content method: `references/content-planning.md`; local-context provenance and fit: `references/local-context-candidates.md`. Reuse product understanding and stage responsibilities, not historical automatic scoring, visual-first ranking or fixed two-line templates.
- User confirmed prior Malaysian-local review of the scene library. Preserve that provenance without claiming Thai-local expertise, current policy validation or verified product effects.
- Public migration tools and documentation: `references/migrations/senty-20261107/README.md`. Raw jobs, product inputs, reference cards, media, source inventories and machine-local audit reports remain local-only. Do not commit them to this public repository. Synthetic tests must run without them or the former Senty application.
- Fixes cover sealed archive contents/derived JSON, added or missing source files, credential file discovery, decoded JSON keys/values, duplicate members and Unicode-escaped references. Media-pattern exclusions apply only to the matched body, not a whole line. Never log credential values or automatically reseal changed content.
- The user approved publishing only public-safe Senty changes and explicitly accepted the existing dependency-audit CI blocker for this push. Do not silently waive findings or upgrade dependencies in this batch. Production source, scoring, GT and model routes are unchanged; no refreeze is needed.
- Never equate local repository deletion with hosted-project shutdown. No hosted-project mutation was authorized or performed. Validation and publication are separate from business quality, adoption or accuracy claims.
