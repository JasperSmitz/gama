"""Genre representation (pure; no mutagen, no Django).

Parsing of stored values into a ``GenreField``, the comparison key, input
validation and the add/remove/replace operations (plan section 1.3).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from gama.metadata.model import GenreField

SEPARATOR = ";"
_DELIM_RE = re.compile(r"\s*;\s*")
_WS_RE = re.compile(r"\s+")


def key(genre: str) -> str:
    """Comparison key: NFC, trimmed, whitespace collapsed, casefolded, NFC again.

    casefold() can produce non-NFC text (e.g. Greek ΐ vs Ϊ́), so the result is
    normalised once more to keep canonically equivalent spellings equal.
    """
    folded = _WS_RE.sub(" ", unicodedata.normalize("NFC", genre).strip()).casefold()
    return unicodedata.normalize("NFC", folded)


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


# --------------------------------------------------------------------------- M3: validation and operations

MAX_GENRE_LENGTH = 255
_JOINERS = ("\u200c", "\u200d")  # ZWNJ, ZWJ
MAX_REPLACE = 20
OPERATIONS = ("add", "remove", "replace")


class GenreError(ValueError):
    """Invalid genre input."""


def validate_genre(raw: str) -> str:
    """Return the genre trimmed, or raise GenreError.

    Refused: empty after trimming, ';' (Gama's only separator), NUL or any
    other control character, more than 255 characters.
    """
    if not isinstance(raw, str):
        raise GenreError("genre must be text")
    # Checked before trimming: str.strip() would silently eat \t, \n, \x1c-\x1f.
    for i, c in enumerate(raw):
        cat = unicodedata.category(c)
        if cat == "Cc":
            raise GenreError("genre may not contain control characters")
        if cat in ("Cs", "Co", "Cn", "Zl", "Zp"):  # Cs (lone surrogates) is also all that UTF-8 cannot encode
            raise GenreError(f"genre may not contain U+{ord(c):04X} (unassigned, private-use, surrogate or "
                             "line/paragraph separator)")
        if cat == "Cf" and not (c in _JOINERS and 0 < i < len(raw) - 1 and not raw[i - 1].isspace()
                                and not raw[i + 1].isspace() and raw[i - 1] not in _JOINERS):
            # Invisible format characters (zero-width space, BOM, bidi overrides,
            # tag characters) would create look-alike duplicates. ZWNJ/ZWJ are
            # allowed only inside a word, where some scripts and emoji need them.
            raise GenreError(f"genre may not contain the invisible character U+{ord(c):04X}")
    g = raw.strip()
    if not g:
        raise GenreError("genre is empty")
    if SEPARATOR in g:
        raise GenreError("genre may not contain ';'")
    if len(g) > MAX_GENRE_LENGTH:
        raise GenreError(f"genre is longer than {MAX_GENRE_LENGTH} characters")
    return g


def validate_request(operation: str, raw: Sequence[str]) -> tuple[str, ...]:
    """Validate the genres of one request. add/remove take exactly one genre;
    replace takes 1..20, deduplicated by key (first spelling wins)."""
    if operation not in OPERATIONS:
        raise GenreError(f"unknown operation {operation!r}")
    # A bare string is a Sequence[str] too: "Rock" must never become R, o, c, k.
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, (list, tuple)):
        raise GenreError("genres must be given as a list")
    gs = [validate_genre(g) for g in raw]
    if operation in ("add", "remove"):
        if len(gs) != 1:
            raise GenreError(f"{operation} takes exactly one genre")
        return (gs[0],)
    if not gs:
        raise GenreError("replace needs at least one genre (use remove to delete genres)")
    out: list[str] = []
    seen: set[str] = set()
    for g in gs:
        if key(g) not in seen:
            seen.add(key(g))
            out.append(g)
    if len(out) > MAX_REPLACE:
        raise GenreError(f"replace takes at most {MAX_REPLACE} genres")
    return tuple(out)


@dataclass(frozen=True)
class OpResult:
    """Outcome of one operation on one file's GenreField.

    values:     the new stored values (``()`` = delete the genre field).
    components: what the user will see afterwards.
    changed:    new values differ from the stored ones.
    notes:      codes explaining a no-op or a side effect ("case-differs", ...).
    refused:    a reason code when the result is unsafe to write.
    """
    values: tuple[str, ...]
    components: tuple[str, ...]
    changed: bool
    notes: tuple[str, ...] = ()
    refused: str | None = None  # the result must not be written (planner turns it into a skip)


def apply(field: GenreField, operation: str, genres: Sequence[str]) -> OpResult:
    """Apply a validated request to one file's genres. Pure; never touches files.

    * add G:      append G unless a component has the same key (existing
                  spelling wins; a case difference is noted).
    * remove G:   drop every component with G's key (case variants and
                  duplicates); nothing left means the field is deleted.
    * replace Gs: the result is exactly Gs.
    Unrelated components are kept exactly as stored. Native values keep their
    stored form (an ID3 "(17)" stays "(17)"); delimited files stay delimited.
    """
    if field.style == "mixed":
        raise GenreError("mixed-style genres are never edited")
    genres = validate_request(operation, genres)
    notes: list[str] = []
    native = field.style != "delimited"
    # Pair each stored entry with what the user sees: for native style one entry
    # per stored value (so "(17)" survives untouched), for delimited one per component.
    pairs = list(zip(field.values, field.components)) if native else [(c, c) for c in field.components]

    if operation == "add":
        g = genres[0]
        match = [shown for _, shown in pairs if key(shown) == key(g)]
        if match:
            notes.append("already-present" if g in match else "case-differs")
            new_pairs = pairs
        else:
            new_pairs = pairs + [(g, g)]
    elif operation == "remove":
        g = genres[0]
        new_pairs = [(s, shown) for s, shown in pairs if key(shown) != key(g)]
        if len(new_pairs) == len(pairs):
            notes.append("not-present")
        else:
            if new_pairs and all(not shown.strip() for _, shown in new_pairs):
                # Only empty values would remain: the user sees no genres, so do not
                # leave an explicit empty genre field behind.
                new_pairs = []
                notes.append("empty-values-dropped")
            if not new_pairs:
                notes.append("field-deleted")
    else:
        new_pairs = [(g, g) for g in genres]

    stored = [s for s, _ in new_pairs]
    if native:
        values = tuple(stored)
    else:  # delimited files keep their single value and their delimiter
        values = ((field.delimiter or SEPARATOR).join(stored),) if stored else ()
    changed = values != field.values
    # A numeric ID3 reference ("(17)") that is gone while its name ("Rock") is
    # still wanted will now be stored as the name.
    kept_keys = {key(shown) for _, shown in new_pairs}
    if changed and any(st != shown and (st, shown) not in new_pairs and key(shown) in kept_keys
                       for st, shown in pairs):
        notes.append("numeric-reference-stored-as-name")
    refused = None
    if changed and not native and values and not delimited_round_trips(parse_field(values)):
        # e.g. "Rock ; " + Jazz -> "Rock ;  ; Jazz", which the parser would read with a
        # different delimiter: after the write Gama itself would refuse the file.
        refused = "would-not-round-trip"
    return OpResult(values, tuple(shown for _, shown in new_pairs), changed, tuple(notes), refused)
