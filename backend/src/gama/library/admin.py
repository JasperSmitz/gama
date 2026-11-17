from django.contrib import admin

from .models import Album, LibraryRoot, MediaFile, Track


@admin.register(LibraryRoot)
class LibraryRootAdmin(admin.ModelAdmin):
    list_display = ("name", "path")


@admin.register(MediaFile)
class MediaFileAdmin(admin.ModelAdmin):
    list_display = ("relpath", "root", "format", "read_ok", "writable_reason", "scanned_at")
    list_filter = ("root", "format", "read_ok")
    search_fields = ("relpath",)


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ("album", "albumartist", "album_dir", "track_count", "disc_count", "inconsistent")
    list_filter = ("root", "inconsistent")
    search_fields = ("album", "albumartist", "album_dir")


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ("title", "artist", "album", "discnumber", "tracknumber", "genre_style")
    search_fields = ("title", "artist", "file__relpath")
