"""Values only a person can supply, asked for at a terminal.

Each secret is named with what it's for, and each field with what to enter. A secret field is
typed into a hidden prompt, a field that isn't secret, such as an account's email, into one
that shows it, and a key or certificate is read from a file whose path is typed. A component
whose engine sends no description, hint or `secret` has every field hidden. The values go to
the component as one JSON document on its stdin and live only in memory: never in arguments,
the environment, files or logs.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from .outcome import Stop
from .screen import Screen


def label(field: dict) -> str:
    """A field's prompt: its name, then what to enter and how it's entered."""
    notes = [field["hint"]] if field.get("hint") else []
    if field.get("file"):
        notes.append("the file's path")
    elif field.get("secret", True):
        notes.append("hidden")
    return f"{field['name']} ({'; '.join(notes)})" if notes else field["name"]


def supply(screen: Screen, needed: list[dict]) -> dict[str, dict[str, str]]:
    """Ask for each secret's fields; ``needed`` is a values stop's `supply` list."""
    values: dict[str, dict[str, str]] = {}
    for secret in needed:
        name = secret["secret"]
        screen.blank()
        screen.labelled("Secret Name:", Text(name, style="bold"))
        if secret.get("description"):
            screen.labelled("Secret Desc:", secret["description"])
        for field in secret.get("fields", []):
            what = f"{name} {field['name']}"
            if field.get("file"):
                path = Path(screen.text(label(field)).strip().strip('"')).expanduser()
                try:
                    value = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    raise Stop("values", f"{path} can't be read as a text file for {what}.") from None
            elif field.get("secret", True):
                value = screen.secret(label(field))
            else:
                value = screen.text(label(field))
            if not value.strip():
                raise Stop("values", f"No value was given for {what}.")
            values.setdefault(name, {})[field["name"]] = value
    return values
