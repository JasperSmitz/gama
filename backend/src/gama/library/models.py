"""Index models (plan section 2). The files on disk are authoritative; these rows
are rebuilt by scans and are only a selection aid."""
from django.db import models


class LibraryRoot(models.Model):
    path = models.CharField(max_length=4096, unique=True)
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class MediaFile(models.Model):
    root = models.ForeignKey(LibraryRoot, on_delete=models.CASCADE, related_name="files")
    relpath = models.CharField(max_length=4096)
    size = models.BigIntegerField()
    mtime_ns = models.BigIntegerField()
    inode = models.BigIntegerField()
    format = models.CharField(max_length=32, null=True, blank=True)
    detected_by = models.CharField(max_length=32, default="content")
    writable_reason = models.CharField(max_length=64, null=True, blank=True,
                                       help_text="Skip reason; null means Gama may write this file")
    read_ok = models.BooleanField(default=False)
    read_error = models.TextField(null=True, blank=True)
    scanned_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["root", "relpath"], name="unique_root_relpath")]

    def __str__(self):
        return self.relpath


class Album(models.Model):
    root = models.ForeignKey(LibraryRoot, on_delete=models.CASCADE, related_name="albums")
    identity_key = models.CharField(max_length=40, unique=True)
    album_dir = models.CharField(max_length=4096)
    album = models.CharField(max_length=1024, blank=True)
    albumartist = models.CharField(max_length=1024, blank=True)
    mb_albumid = models.CharField(max_length=64, blank=True)
    track_count = models.PositiveIntegerField(default=0)
    disc_count = models.PositiveIntegerField(default=0)
    formats = models.JSONField(default=list)
    genres_summary = models.JSONField(default=dict)
    inconsistent = models.BooleanField(default=False)

    class Meta:
        ordering = ["albumartist", "album", "album_dir"]

    def __str__(self):
        return f"{self.albumartist} - {self.album}"


class Track(models.Model):
    file = models.OneToOneField(MediaFile, on_delete=models.CASCADE, related_name="track")
    album = models.ForeignKey(Album, on_delete=models.SET_NULL, null=True, related_name="tracks")
    title = models.CharField(max_length=1024, blank=True)
    artist = models.CharField(max_length=1024, blank=True)
    albumartist = models.CharField(max_length=1024, blank=True)
    album_title = models.CharField(max_length=1024, blank=True, db_column="album")
    discnumber = models.CharField(max_length=32, blank=True)
    tracknumber = models.CharField(max_length=32, blank=True)
    mb_albumid = models.CharField(max_length=64, blank=True)
    genres = models.JSONField(default=list)
    genre_style = models.CharField(max_length=16, blank=True)
    warnings = models.JSONField(default=list)

    class Meta:
        ordering = ["file__relpath"]

    def __str__(self):
        return self.title or self.file.relpath
