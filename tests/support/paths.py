"""Repository locations shared by tests.

Tests resolve fixtures and repository files through these constants instead of walking up from
their own ``__file__`` so that a test keeps working wherever it lives under ``tests/``.
"""

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

__all__ = ["FIXTURES_DIR", "REPO_ROOT", "TESTS_DIR"]
