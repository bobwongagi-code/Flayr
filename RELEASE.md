# Release Process

1. Update `VERSION` and add the corresponding entry to `CHANGELOG.md`.
2. Review `requirements-dev.lock`; dependency upgrades must be exact pins and
   must not reintroduce removed video-generation or voice-cloning SDKs.
3. Run `python3 scripts/check_release.py` and `bash scripts/run-tests.sh`.
4. Create the release tag `v<version>` from a clean, reviewed commit.
5. Keep the generated run directories and local job manifests out of the
   release commit.

The repository does not publish a Python package. Releases are source/workflow
releases, and the version in `VERSION` is the single source for the release
identifier.

## Scope Checks

Local implementation batches use `scripts/run-tests.sh` with an explicit
`FLAYR_SCOPE_SPEC` and `FLAYR_SCOPE_BASE_REF`; the default `batch` mode enforces
that batch's path allowlist and file/line budgets.

CI uses `FLAYR_SCOPE_MODE=publication` for the full push/PR range. A cumulative
range is not one implementation batch: CI retains the forbidden-path checks
for run outputs, local job manifests and cache files without applying an old
batch's allowlist or budget. Initial pushes check the entire tree. Full tests,
semantic freeze verification and quality gates still run; this mode does not
authorize business-rule changes or replace local batch review.
