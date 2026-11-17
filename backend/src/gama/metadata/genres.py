"""Genre representation (pure; no mutagen, no Django).

M2 provides parsing of stored values into a ``GenreField`` and the comparison
key. The add/remove/replace operations and input validation follow in M3.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence

from gama.metadata.model import GenreField

SEPARATOR = ";"
_DELIM_RE = re.compile(r"\s*;\s*")
_WS_RE = re.compile(r"\s+")


def key(genre: str) -> str:
    """Comparison key: NFC, trimmed, whitespace collapsed, casefolded."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", genre).strip()).casefold()


def parse_field(values: Sequence[str], resolve: Callable[[str], str] | None = None) -> GenreField:
    """Build a GenreField from the stored values of one file.

    ``resolve`` maps one stored native value to its display name (used for ID3
    numeric references such as "(17)" -> "Rock"); identity by default.

    * no values                      -> empty
    * one value containing ";"       -> delimited (split and trimmed; the first
                                        separator, with its spacing, is kept)
    * several values, one with ";"   -> mixed (problematic; never written)
    * otherwise                      -> native
    "/" and "," are never separators.
    """
    vals = tuple(values)
    if not vals:
        return GenreField((), "empty", None, ())
    if len(vals) == 1 and SEPARATOR in vals[0]:
        m = _DELIM_RE.search(vals[0])
        comps = tuple(c.strip() for c in vals[0].split(SEPARATOR))
        return GenreField(vals, "delimited", m.group(0), comps)
    if any(SEPARATOR in v for v in vals):
        return GenreField(vals, "mixed", None, vals)
    resolve = resolve or (lambda v: v)
    return GenreField(vals, "native", None, tuple(resolve(v) for v in vals))


def delimited_round_trips(g: GenreField) -> bool:
    """True if re-joining the components with the delimiter gives the stored value.

    A value like "Rock;Pop; Jazz" (inconsistent spacing) or " Rock;Pop"
    (padding) would change unrelated components on rewrite, so policy refuses it.
    """
    return g.style != "delimited" or (g.delimiter or "").join(g.components) == g.values[0]


def has_duplicates(g: GenreField) -> bool:
    keys = [key(c) for c in g.components if c.strip()]
    return len(keys) != len(set(keys))


def has_empty(g: GenreField) -> bool:
    return any(not c.strip() for c in g.components)
