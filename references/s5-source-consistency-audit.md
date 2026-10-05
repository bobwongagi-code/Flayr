# S5 source-record consistency gate

Date: 2026-10-05
State: implemented, committed as `d4c009ef1061a6bcb076587cb2b0de47f3c31eb3`, and mechanically refrozen after authorization on continuation. Fixed-source verification passes. Not approved for production route activation or accuracy promotion.
Parent source: `4abd9086bec14a426654d43b9a9a8a60909c4fe4`.

## Defect and scope

S5 could close as `absent` or `not_applicable` despite structured records containing a trust source, an unanswered visual endorsement, or an incomplete source reference. This is a negative-closure consistency defect, not proof that all S5 errors share this cause.

The guard rejects those negative closures. Existing recovery planning receives the S5 issue; existing unresolved-stage blocking preserves `unknown`. It does not fill source types, qualify a source, change positive eligibility thresholds, change prompts or magnitude definitions, change routes, or add retry loops. Atomic facts remain unchanged.

This tightens negative-result acceptance. More unresolved S5 results are an expected possible consequence, not evidence of improved model accuracy.

## Verification

- Before implementation, six of the original ten regression cases failed.
- All 13 new regression cases pass, including existing recovery planning and unresolved-stage blocking.
- The existing evidence-contract suite passes: 200 tests.
- Full suite: 1,111 tests, with only `test_repository_freeze_contract_is_consistent` failing because the production semantic source differs from its frozen commit. The remaining 1,110 tests pass. This is not a complete release-gate pass.
- Prompt reachability and result-field ownership checks pass. Analysis-contract verification reports the same freeze drift.
- The new test file passes Ruff. The production file retains five pre-existing Ruff findings; comparison with the parent source found no additional findings. Unrelated lint was not changed.
- Historical creator S5 negative closure is now rejected with `S5:negative_with_unresolved_visual_source:C4`.
- Both previously usable positive qualification responses remain valid through the existing normalization path. Their raw responses contain optional unbound signals; the existing normalizer records those as unqualified observations. Raw JSON is not claimed flawless.

The default test scope belongs to an older, narrower batch. This batch uses `references/s5-source-consistency-scope.json`; release, invariant and scope checks pass under that scope. Source freezing remains unresolved.

Full-suite log: `runs/diagnostics/gemini37-adaptation-20261005/s5-gate-test.log` (local diagnostic, not a tracked fixture).

## Boundaries

Explicitly incorrect subjects are excluded from this source-conflict check. Explicitly absent verbal sources can remain absent, because a negated certification mention is not positive evidence. Unflagged legacy records are not automatically blocked merely for lacking newer fields.

The guard cannot discover an unrecognized mark, correct OCR, or infer a source from prose alone. It does not validate certification authenticity or product efficacy. No new model requests, historical reruns, route switches or GitHub pushes were performed for this fix.

## Remaining version gate

The initial freeze failure above is preserved as pre-refreeze evidence. After source commit and authorized mechanical binding, the full project runner passes: all 1,111 tests, release checks, invariant checks, scoped-change gate, prompt reachability, analysis contracts (including freeze identity), and result-field ownership.

Final full-suite log: `runs/diagnostics/gemini37-adaptation-20261005/s5-fixed-source-test.log`. The test run used committed production source; only the reviewed refreeze metadata was pending its separate commit. The freeze diff changes only source identity, execution-readiness explanation and latest-refreeze history. GT snapshots, routes, thresholds, schema identities and budgets are unchanged.

No failed rubric candidate, historical score migration, provider request or production activation is included. No GitHub push is authorized or performed.
