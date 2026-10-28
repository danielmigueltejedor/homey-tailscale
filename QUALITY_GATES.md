# AI engineering quality contract

An agent must provide actual check output, add tests for new behavior, and never reduce test assertions, coverage limits, or CI checks to force a pass. Raise policy changes for explicit human review.

Enforced: `git diff --check`; AST parsing of all tracked Python modules.

Make `quality` a required check in repository branch protection to block merges. This workflow **does not** claim unconfigured coverage or performance thresholds. Further language-specific gates should use measured baselines.
