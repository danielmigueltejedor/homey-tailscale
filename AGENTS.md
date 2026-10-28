# Agent instructions — homey-tailscale

## Repository-specific deterministic quality contract (2026-10-26)

**Project:** Homey Pro Python SDK application packaging Tailscale binaries.

**Engineering requirements:** Preserve Homey App Store manifest and target architecture, CLI status/route handling and userspace networking. Do not ship binaries with unknown origin.

**Verification commands:** python3 -m compileall -q .; execute targeted unit tests and app packaging validation from scripts.

Do not assert any test passed without actual output. Add tests for changed behavior, malformed input and regressions. Never remove/weaken tests, reduce thresholds, suppress errors, or change CI merely to produce a green result. Require review of `QUALITY_GATES.md`, workflow and contract edits. Make the `quality` check required via branch protection. Native tooling blockers must be reported explicitly; preserve existing agent instructions.
