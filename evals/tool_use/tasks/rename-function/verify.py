"""Pass only when every reference moved to display_name and get_user_name_parts kept its name."""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

ADA = {"given": "Ada", "family": "Lovelace", "email": "ada@example.com"}
ANON = {"email": "anon@example.com"}
OLD_NAME = re.compile(r"\bget_user_name\b(?!_parts)")


def check() -> list[str]:
    import accounts
    from accounts import columns, emails, export, greetings, names

    problems = []
    if hasattr(names, "get_user_name") or hasattr(accounts, "get_user_name"):
        problems.append("get_user_name still exists")
    if not hasattr(names, "get_user_name_parts") or hasattr(names, "display_name_parts"):
        problems.append("get_user_name_parts was renamed")
    if names.display_name(ADA) != "Ada Lovelace" or accounts.display_name(ANON) != ANON["email"]:
        problems.append("display_name is wrong")
    if "display_name" not in accounts.__all__ or "get_user_name" in accounts.__all__:
        problems.append("__all__ not updated")
    if columns.render_row(ANON)[0] != ANON["email"]:
        problems.append("admin columns broken")
    if greetings.greeting(ADA) != "Hello, Ada Lovelace!":
        problems.append("greeting broken")
    if not emails.subject_line(ADA, "hi").startswith("Ada Lovelace"):
        problems.append("emails broken")
    if "Ada Lovelace" not in export.to_csv([ADA]):
        problems.append("export broken")
    for path in Path().rglob("*.py"):
        if OLD_NAME.search(path.read_text(encoding="utf-8")):
            problems.append(f"{path} still mentions get_user_name")
    tests = subprocess.run([sys.executable, "-m", "unittest", "-q"], capture_output=True)
    if tests.returncode != 0:
        problems.append("project tests fail")
    return problems


try:
    problems = check()
except Exception as err:  # noqa: BLE001 - any import or call failure fails the task
    problems = [f"{type(err).__name__}: {err}"]
print("\n".join(problems) or "ok")
sys.exit(1 if problems else 0)
