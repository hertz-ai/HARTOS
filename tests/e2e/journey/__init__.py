"""Journey coverage — bucket 3 of memory/user_journey_coverage.md.

Each test_j<range>_<slug>.py file covers one 10-journey cluster.
Every test either asserts a real contract OR skips with a specific
missing-module reason.  Skips are the living gap dashboard — never
masked with pass.

Picked up automatically by:
  - .github/workflows/release.yml  (tests/e2e/ best-effort step)
  - scripts/run_regression.sh      (JOURNEY_TESTS group)
  - scripts/run_regression.bat     (JOURNEY_TESTS group)
"""
