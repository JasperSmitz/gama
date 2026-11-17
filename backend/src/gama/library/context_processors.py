from . import basket


def basket_summary(request):
    """The selected albums, shown on every page so the selection stays visible."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"basket_albums": basket.albums(request)}
