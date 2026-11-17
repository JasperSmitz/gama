"""The album selection ("basket"), kept in the session as a list of album ids.

Ids of albums that no longer exist (removed by a rescan) are dropped on read.
"""
from __future__ import annotations

from .models import Album

KEY = "gama_basket"
MAX_ALBUMS = 500


def ids(request) -> list[int]:
    raw = request.session.get(KEY, [])
    out = [i for i in raw if isinstance(i, int)]
    existing = set(Album.objects.filter(id__in=out).values_list("id", flat=True))
    kept = [i for i in out if i in existing]
    if kept != raw:
        request.session[KEY] = kept
    return kept


def albums(request):
    order = {i: n for n, i in enumerate(ids(request))}
    return sorted(Album.objects.filter(id__in=order), key=lambda a: order[a.id])


def add(request, new_ids) -> int:
    cur = ids(request)
    valid = set(Album.objects.filter(id__in=new_ids).values_list("id", flat=True))
    added = [i for i in new_ids if i in valid and i not in cur]
    cur = (cur + added)[:MAX_ALBUMS]
    request.session[KEY] = cur
    return len(added)


def remove(request, gone_ids) -> None:
    request.session[KEY] = [i for i in ids(request) if i not in set(gone_ids)]


def clear(request) -> None:
    request.session[KEY] = []
