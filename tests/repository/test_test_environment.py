from __future__ import annotations

import os


def test_inherited_wisp_environment_is_cleared() -> None:
    assert sorted(name for name in os.environ if name.startswith("WISP_")) == []
