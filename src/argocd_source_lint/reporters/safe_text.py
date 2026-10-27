from __future__ import annotations

import re

# C0 and C1 control characters, minus tab and newline. Repo content (an
# Application name, a path, a scan_roots entry) can carry them through a
# YAML "\e" escape: printed raw, ESC sequences restyle or clear the
# terminal of whoever runs the tool, and XML 1.0 rejects most of them,
# breaking the JUnit report. Rich's `escape()` only covers its own markup.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def visible_controls(text: str) -> str:
    """Replaces each control character with its `\\xNN` spelling."""
    return _CONTROL_CHARACTERS.sub(lambda match: f"\\x{ord(match.group()):02x}", text)
