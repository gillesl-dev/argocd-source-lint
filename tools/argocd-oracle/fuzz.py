"""Differential fuzzing of gotemplate against Argo CD's own Render.Replace.

Random params (Unicode strings, boundary numbers, nested lists and maps) go
through toYaml, toJson, mustToPrettyJson, quote, printf %v/%s/%q, printing and
field access, in the oracle and in this tool. The rule: the same output, the
same failure, or a refusal by this tool; never a different output.

Needs Docker, like regenerate.py. Run from the repository root:

    uv run python tools/argocd-oracle/fuzz.py [count] [seed]

Prints the counts and writes the differences, if any, to fuzz-differences.json
in the current directory.
"""

from __future__ import annotations

import collections
import json
import random
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from regenerate import run_oracle  # noqa: E402

from argocd_source_lint import gotemplate  # noqa: E402

_CHARS = (
    "abcxyzABCXYZ0189 _-./:#'\"\\%@&*!|>?,[]{}~`<>=+\t\n\r"
    "éßÀñ\u00a0\u00ad漢字한😀\u0301\u200b\u2028\u2029\u0085\ufeff\ue000\U000105c0\U00011f04"
    "\x01\x1f\x7f\u0378\uffff"
)
_TOKENS = [
    "", " ", "yes", "no", "on", "off", "y", "n", "true", "False", "null", "~", "NULL",
    "1", "-1", "0", "007", "0x1F", "0o17", "0b101", "1e3", "1.5", ".5", "-.inf", ".nan",
    "1:20", "1_000", "2024-01-02", "2024-1-2", "2024-02-30", "2024-01-02T10:00:00Z",
    "- a", "a: b", "a:b", "#x", "a #x", "*x", "&x", "!x", "%x", "@x", "`x", "?", "? x",
    "---", "...", "x ", " x", "a\nb", "a\n", "a\n\n", "\n", "line1\n  line2", "'q'", '"q"',
]  # fmt: skip
_NUMBERS = [
    0.0, -0.0, 1.0, -1.0, 0.5, 1e6, 1e20, 1e21, 1e22, 1e-6, 1e-7, 1e-5, 2.0**53, 2.0**63,
    2.0**64, 123456.789, 1.7976931348623157e308, 5e-324, 100000.0, 999999.0, 1234567.0,
    0.1, 0.30000000000000004, -123.456e-10,
]  # fmt: skip


def _string(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.25:
        return rng.choice(_TOKENS)
    if roll < 0.45:
        # Tokens side by side: "a #x", "x: y", "1 - 2"...
        parts = [rng.choice([*_TOKENS, "a", "x", "1"]) for _ in range(rng.randint(2, 3))]
        return rng.choice(["", " ", ": ", " #", "-", "\n"]).join(parts)
    if roll < 0.55:
        words = [rng.choice(["alpha", "beta", "gamma", "x", "deploy-1"]) for _ in range(30)]
        return " ".join(words)[: rng.randint(60, 140)]
    return "".join(rng.choice(_CHARS) for _ in range(rng.randint(0, 12)))


def _number(rng: random.Random) -> float:
    roll = rng.random()
    if roll < 0.4:
        return rng.choice(_NUMBERS) * rng.choice([1, -1])
    if roll < 0.7:
        return float(rng.randint(-(10 ** rng.randint(0, 19)), 10 ** rng.randint(0, 19)))
    return rng.random() * 10 ** rng.randint(-12, 25) * rng.choice([1, -1])


def _value(rng: random.Random, depth: int = 0) -> Any:
    roll = rng.random()
    if depth < 3 and roll < 0.2:
        return [_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if depth < 3 and roll < 0.4:
        return {_string(rng): _value(rng, depth + 1) for _ in range(rng.randint(0, 4))}
    if roll < 0.7:
        return _string(rng)
    if roll < 0.9:
        return _number(rng)
    return rng.choice([True, False, None])


_FAMILIES = {
    "toYaml": "{{ toYaml .v }}",
    "toJson": "{{ toJson .v }}",
    "mustToPrettyJson": "{{ mustToPrettyJson .v }}",
    "quote": "{{ quote .v }}",
    "printf %v": '{{ printf "%v" .v }}',
    "printf %s": '{{ printf "%s" .v }}',
    "printf %q": '{{ printf "%q" .v }}',
    "print": "{{ .v }}",
}


def _field_case(rng: random.Random) -> tuple[str, dict[str, Any]]:
    keys = [rng.choice(["a", "b", "name", "x1", "_y", "é", "k-1", "1"]) for _ in range(3)]
    value = {key: _value(rng, 1) for key in keys}
    path = ".".join(rng.choice([*keys, "missing"]) for _ in range(rng.randint(1, 3)))
    template = rng.choice(["{{ .v.%s }}", "{{ with .v }}{{ .%s }}{{ end }}"]) % path
    return template, {"v": value}


def _cases(count: int, seed: int) -> list[tuple[str, str, dict[str, Any]]]:
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        family = rng.choice([*_FAMILIES, "field"])
        if family == "field":
            template, params = _field_case(rng)
        else:
            template, params = _FAMILIES[family], {"v": _value(rng)}
        cases.append((family, template, params))
    return cases


def _as_json(value: Any) -> Any:
    """What the controller receives: JSON, with float64 numbers."""
    return json.loads(json.dumps(value), parse_int=float)


def _tool(template: str, params: dict[str, Any]) -> tuple[str, str | None]:
    try:
        return "output", gotemplate.render(template, _as_json(params))
    except gotemplate.UnsupportedTemplateError:
        return "refused", None
    except gotemplate.ArgoTemplateError:
        return "error", None


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261124
    cases = _cases(count, seed)
    renders = [{"template": t, "params": p, "options": []} for _, t, p in cases]
    with tempfile.TemporaryDirectory() as tmp:
        (oracle,) = run_oracle({"appsets": [], "renders": renders}, Path(tmp))
    totals: collections.Counter[str] = collections.Counter()
    by_family: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    differences = []
    for (family, template, params), expected in zip(cases, oracle["renders"], strict=True):
        kind, output = _tool(template, params)
        if kind == "refused":
            verdict = "refused"
        elif kind == "error" and "error" in expected:
            verdict = "identical"
        elif kind == "output" and expected.get("output") == output:
            verdict = "identical"
        else:
            verdict = "different"
            differences.append(
                {"template": template, "params": params, "tool": [kind, output],
                 "argo": expected}
            )  # fmt: skip
        totals[verdict] += 1
        by_family[family][verdict] += 1
    print(f"{count} inputs (seed {seed}): {dict(totals)}")
    for family, counts in sorted(by_family.items()):
        print(f"  {family:18} {dict(counts)}")
    Path("fuzz-differences.json").write_text(
        json.dumps(differences, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
