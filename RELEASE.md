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
