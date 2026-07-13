# Releasing

Releases are published from GitHub Actions through PyPI Trusted Publishing.
There is no long-lived PyPI token in GitHub or on a maintainer machine.

## One-time PyPI publisher

The PyPI project uses these exact pending publisher values:

| Field | Value |
|---|---|
| PyPI project name | `claude-usage-tui` |
| GitHub owner | `ivanopcode` |
| GitHub repository | `claude-usage` |
| Workflow filename | `publish.yml` |
| GitHub environment | `pypi` |

The GitHub `pypi` environment and workflow filename are part of the OIDC
identity. Changing either requires updating the publisher on PyPI first.
Deployments to that environment are restricted to Git tags matching `v*`.

## Publish a version

1. Update `collector.VERSION` and add the matching section to `CHANGELOG.md`.
2. Run `uv lock`, the unit tests, and `uv build`.
3. Commit and push the release preparation.
4. Create and push a signed tag matching the version exactly:

   ```bash
   git tag -s v0.2.0 -m "Release v0.2.0"
   git push origin v0.2.0
   ```

The `Publish` workflow then:

1. verifies that the tag equals `v` plus `collector.VERSION`;
2. runs the tests and builds both sdist and wheel;
3. validates and smoke-installs the wheel;
4. publishes to PyPI using a short-lived OIDC credential;
5. creates a GitHub Release with both distributions attached.

PyPI versions and Git tags are immutable. If publishing fails after a version
has reached PyPI, fix the issue and release a new patch version; never move or
reuse a published tag.
