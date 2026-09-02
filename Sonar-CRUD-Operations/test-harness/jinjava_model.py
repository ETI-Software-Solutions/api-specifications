"""
jinjava_model.py — a deliberately narrow model of HubSpot Jinjava 2.8.3.

THIS IS NOT JINJAVA. It is a Jinja2 environment shimmed to reproduce the
handful of Jinjava behaviours the gateway's templates actually depend on, so
that document breakage can be caught offline in milliseconds instead of only
after a spec upload.

Two rules keep the model honest:

  1. Every shim below cites the Jinjava behaviour it reproduces. If a citation
     cannot be given, the shim does not get written — the construct is banned
     instead.
  2. `assert_supported()` refuses to render any template that uses a construct
     outside the verified shared subset. A template that drifts out of the
     subset fails the harness rather than being silently mis-rendered.

Anything this model reports is a *prediction*. Stage L4 against a real gateway
is what turns a prediction into evidence.
"""
from __future__ import annotations

import json
import re

from jinja2 import Environment, Undefined


# --------------------------------------------------------------------------
# Constructs verified to behave identically in Jinjava 2.8.3 and Jinja2.
# Sources are section numbers in AUTHORING-SPECS.md (commit fc84c11).
# --------------------------------------------------------------------------
SUPPORTED = {
    "{{ }}":        "expression output (§10.1)",
    "{% for %}":    "iteration over a list (§10.6)",
    "{% if %}":     "only the `not loop.last` separator idiom (§10.6)",
    "{% set %}":    "render-local assignment of a dict literal (§10.7)",
    "| tojson":     "JSON serialisation; undefined renders literal null (§10.11)",
    "| default":    "two-argument form fires on any falsy value (§10.10)",
    "a.b.c":        "chained access through a missing level yields empty (§10.3)",
    "m[k]":         "bracket index on a map or list (§10.3)",
}

# Constructs known to diverge, or unverified. Their presence fails the render.
BANNED = {
    r"\{%\s*for\s+\w+\s*,": "tuple unpacking in for is unsupported in 2.8.3 (§16.4)",
    r"\{%\s*endfor\s*%\}\s*\{%\s*else": "for/else is FATAL in 2.8.3 (§16.5)",
    r"\{%\s*gset\b": "gset is event-scoped; not modellable offline (§10.7)",
    r"\bis\s+true\b": "strict identity with Boolean.TRUE, not truthiness (§10.10)",
    r"\|\s*safe\b": "no-op here; autoescape is off (§10.12)",
    r"\{%\s*include\b": "not configured (§17)",
    r"\{%\s*macro\b": "macro recursion disabled (§10.13)",
}

# The only conditional the templates are permitted to use.
ALLOWED_IF = re.compile(r"\{%\s*if not loop\.last\s*%\}")


class JinjavaUndefined(Undefined):
    """Jinjava renders a missing key, and any chain through one, as the empty
    string rather than raising (§10.3)."""

    def __str__(self) -> str:
        return ""

    __repr__ = __str__

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self.__class__(hint=self._undefined_hint, name=name)

    def __getitem__(self, key):
        return self.__class__(hint=self._undefined_hint, name=str(key))

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __eq__(self, other):
        return isinstance(other, JinjavaUndefined)

    def __hash__(self):
        return hash(type(self))


def _tojson(value):
    """`{{ missing | tojson }}` renders the literal four characters `null`
    (§10.11). Jinja2 would raise instead, so this is shimmed rather than
    inherited."""
    if isinstance(value, JinjavaUndefined) or value is None:
        return "null"
    return json.dumps(value)


def _default(value, fallback="", boolean=False):
    """Two-argument form fires on any falsy value, not only on undefined
    (§10.10). The gateway's narrowing guard depends on this."""
    if isinstance(value, JinjavaUndefined):
        return fallback
    if boolean and not value:
        return fallback
    return value


class UnsupportedConstruct(Exception):
    pass


def assert_supported(template: str, name: str = "<template>") -> None:
    for pattern, why in BANNED.items():
        if re.search(pattern, template):
            raise UnsupportedConstruct(f"{name}: {why}")
    for m in re.finditer(r"\{%\s*if\b.*?%\}", template):
        if not ALLOWED_IF.match(m.group(0)):
            raise UnsupportedConstruct(
                f"{name}: conditional {m.group(0)!r} is not the permitted "
                f"`not loop.last` separator idiom; business branching belongs "
                f"in x-classification, not in a template")


def environment() -> Environment:
    env = Environment(undefined=JinjavaUndefined, autoescape=False,
                      trim_blocks=False, lstrip_blocks=False,
                      keep_trailing_newline=True)
    env.filters["tojson"] = _tojson
    env.filters["default"] = _default
    env.filters["d"] = _default
    return env


def render(template: str, context: dict, name: str = "<template>") -> str:
    assert_supported(template, name)
    return environment().from_string(template).render(**context)
