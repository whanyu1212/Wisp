"""Keep configuration reference fields and JSON examples aligned with settings."""

import json
import re

from tests.support.paths import DOCS_SRC_DIR
from wisp.config.settings import _USER_ONLY_SETTINGS_FIELDS, WispSettings

_REFERENCE = DOCS_SRC_DIR / "reference"


def test_user_only_field_list_matches_settings_policy() -> None:
    document = (_REFERENCE / "configuration.md").read_text(encoding="utf-8")
    section = document.split("## User-only fields\n", 1)[1].split("\n## ", 1)[0]
    # The field list uses middle dots; later prose also names project-allowed fields.
    field_list = section.split("\n\n")[1]
    assert set(re.findall(r"`(\w+)`", field_list)) == _USER_ONLY_SETTINGS_FIELDS


def test_reference_settings_examples_validate() -> None:
    document = (_REFERENCE / "configuration.md").read_text(encoding="utf-8")
    examples = re.findall(r"```json\n(.*?)\n```", document, re.DOTALL)
    assert examples
    for example in examples:
        data = json.loads(example)
        assert set(data) <= WispSettings.model_fields.keys()
        WispSettings.model_validate(data)
