# Flayr Handoff

## Senty Content Migration (2026-11-07)

- Current product-to-content method: `references/content-planning.md`; local-context provenance and fit: `references/local-context-candidates.md`. Reuse product understanding and stage responsibilities, not historical automatic scoring, visual-first ranking or fixed two-line templates.
- User confirmed prior Malaysian-local review of the scene library. Preserve that provenance without claiming Thai-local expertise, current policy validation or verified product effects.
- Public migration tools and documentation: `references/migrations/senty-20261107/README.md`. Raw jobs, product inputs, reference cards, media, source inventories and machine-local audit reports remain local-only. Do not commit them to this public repository. Synthetic tests must run without them or the former Senty application.
- Fixes cover sealed archive contents/derived JSON, added or missing source files, credential file discovery, decoded JSON keys/values, duplicate members and Unicode-escaped references. Media-pattern exclusions apply only to the matched body, not a whole line. Never log credential values or automatically reseal changed content.
- The user approved publishing only public-safe Senty changes and explicitly accepted the existing dependency-audit CI blocker for this push. Do not silently waive findings or upgrade dependencies in this batch. Production source, scoring, GT and model routes are unchanged; no refreeze is needed.
- Never equate local repository deletion with hosted-project shutdown. No hosted-project mutation was authorized or performed. Validation and publication are separate from business quality, adoption or accuracy claims.
