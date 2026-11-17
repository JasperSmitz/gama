"""The album selection ("basket"), kept in the session as a list of album ids.

Only albums of configured library roots are accepted or returned. Ids of albums
that no longer exist (removed by a rescan) or whose root is no longer
configured are dropped on read; duplicates are removed.
"""
from __future__ import annotations

from .models import visible_albums

KEY = "gama_basket"
MAX_ALBUMS = 500


def _dedupe(seq):
    seen, out = set(), []
    for i in seq:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def ids(request) -> list[int]:
    raw = request.session.get(KEY, [])
    out = _dedupe(i for i in raw if isinstance(i, int) and not isinstance(i, bool))
    existing = set(visible_albums().filter(id__in=out).values_list("id", flat=True))
    kept = [i for i in out if i in existing]
    if kept != raw:
        request.session[KEY] = kept
    return kept


def albums(request):
    order = {i: n for n, i in enumerate(ids(request))}
    return sorted(visible_albums().filter(id__in=order), key=lambda a: order[a.id])


def add(request, new_ids) -> tuple[int, int]:
    """Add albums; returns (number added, number refused because the selection is full)."""
    cur = ids(request)
    valid = set(visible_albums().filter(id__in=new_ids).values_list("id", flat=True))
    wanted = [i for i in _dedupe(new_ids) if i in valid and i not in set(cur)]
    room = max(0, MAX_ALBUMS - len(cur))
    request.session[KEY] = cur + wanted[:room]
    return len(wanted[:room]), len(wanted) - len(wanted[:room])


def remove(request, gone_ids) -> None:
    request.session[KEY] = [i for i in ids(request) if i not in set(gone_ids)]


def clear(request) -> None:
    request.session[KEY] = []
