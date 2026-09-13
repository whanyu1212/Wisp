"""Visual evidence must preserve terminal cells without emitting malformed SVG."""

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from benchmarks.tui_visual_capture import export_rust


def test_export_preserves_wide_cells_and_escapes_markup(tmp_path: Path) -> None:
    cells = [
        {
            "text": text,
            "fg": "#dddddd",
            "bg": "#18181e",
            "bold": False,
            "italic": False,
            "underline": False,
            "reverse": False,
            "strike": False,
        }
        for text in ["界", " ", "<", "&"]
    ]
    path = tmp_path / "screen.json"
    path.write_text(json.dumps({"width": 4, "height": 1, "cells": cells}))
    export_rust(path)
    tree = ElementTree.parse(path.with_suffix(".svg"))
    rendered = "".join(tree.getroot().itertext())
    assert "界&lt;" not in rendered
    assert "界<&" in rendered


def test_export_rejects_incomplete_capture(tmp_path: Path) -> None:
    path = tmp_path / "screen.json"
    path.write_text(json.dumps({"width": 4, "height": 1, "cells": []}))
    with pytest.raises(ValueError, match="cell count"):
        export_rust(path)
