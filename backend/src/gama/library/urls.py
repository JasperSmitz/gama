from django.urls import path

from . import views

app_name = "library"

urlpatterns = [
    path("albums/", views.album_list, name="albums"),
    path("albums/<int:album_id>/", views.album_detail, name="album"),
    path("basket/", views.basket_view, name="basket"),
    path("basket/add/", views.basket_add, name="basket_add"),
    path("basket/remove/", views.basket_remove, name="basket_remove"),
    path("basket/clear/", views.basket_clear, name="basket_clear"),
]
