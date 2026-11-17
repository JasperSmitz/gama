from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import basket
from .models import Album

PAGE_SIZE = 100


def album_list(request):
    q = request.GET.get("q", "").strip()
    qs = Album.objects.all()
    if q:
        qs = qs.filter(Q(album__icontains=q) | Q(albumartist__icontains=q) | Q(album_dir__icontains=q))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "library/album_list.html",
                  {"page": page, "q": q, "selected": set(basket.ids(request))})


def album_detail(request, album_id: int):
    album = get_object_or_404(Album, id=album_id)
    tracks = album.tracks.select_related("file").order_by("discnumber", "tracknumber", "file__relpath")
    return render(request, "library/album_detail.html",
                  {"album": album, "tracks": tracks, "in_basket": album.id in basket.ids(request)})


def basket_view(request):
    return render(request, "library/basket.html", {"albums": basket.albums(request)})


def _ids(request) -> list[int]:
    out = []
    for v in request.POST.getlist("album"):
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out


def _back(request, default: str):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(default)


@require_POST
def basket_add(request):
    n = basket.add(request, _ids(request))
    messages.info(request, f"{n} album(s) added to the selection.")
    return _back(request, "library:basket")


@require_POST
def basket_remove(request):
    basket.remove(request, _ids(request))
    return _back(request, "library:basket")


@require_POST
def basket_clear(request):
    basket.clear(request)
    return _back(request, "library:basket")
