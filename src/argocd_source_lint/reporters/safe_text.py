from __future__ import annotations

import re

# C0 and C1 control characters, minus tab and newline. Repo content can
# carry them through a YAML "\e" escape: printed raw, ESC sequences can
# restyle or clear the user's terminal, and XML 1.0 rejects most of them.
# Rich's escape() only covers its own markup.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def visible_controls(text: str) -> str:
    """Replace each control character with its `\\xNN` spelling."""
    return _CONTROL_CHARACTERS.sub(lambda match: f"\\x{ord(match.group()):02x}", text)
