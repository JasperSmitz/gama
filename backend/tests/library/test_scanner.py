"""M6: scanner and album identity on the synthetic corpus (tmp_path only)."""
import os
import shutil
import subprocess

import pytest

from gama.library.models import Album, LibraryRoot, MediaFile, Track
from gama.library.scanner import album_dir_of, identity_key, norm, scan_root
from gama.metadata.reader import read_file

pytestmark = [pytest.mark.slow, pytest.mark.django_db]

KC = "King Crimson/1969 - In the Court of the Crimson King"


def inproc(paths):
    return [read_file(p) for p in paths]


@pytest.fixture
def root(corpus):
    return LibraryRoot.objects.create(path=str(corpus.root), name="corpus")


def scan(root):
    return scan_root(root, reader=inproc)


def albums_in(dirpath):
    return list(Album.objects.filter(album_dir=dirpath))


# --------------------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("rel,expected", [
    ("A/B/01.flac", "A/B"), ("A/CD1/01.flac", "A"), ("A/Disc 2/01.flac", "A"), ("A/disk_3/01.flac", "A"),
    ("A/cd-10/x.mp3", "A"), ("A/CDs/x.mp3", "A/CDs"), ("A/Discography/x.mp3", "A/Discography"), ("x.flac", ""),
])
def test_album_dir(rel, expected):
    assert album_dir_of(rel) == expected


def test_identity_key_normalisation():
    a = identity_key(1, "d", "  King  Crimson ", "In The Court", "")
    assert a == identity_key(1, "d", "king crimson", "in the court", None)
    assert a != identity_key(1, "d2", "king crimson", "in the court", "")
    assert a != identity_key(2, "d", "king crimson", "in the court", "")
    assert a != identity_key(1, "d", "king crimson", "in the court", "mbid")
    assert norm("Café") == norm("Café")


# --------------------------------------------------------------------------- groupings

def test_scan_indexes_every_audio_file(corpus, root):
    s = scan(root)
    assert s.added == len(corpus.manifest) and s.updated == s.removed == 0
    assert MediaFile.objects.count() == len(corpus.manifest)


def test_example_albums_are_single_albums(corpus, root):
    scan(root)
    for d, artist in ((KC, "King Crimson"), ("Genesis/1973 - Selling England by the Pound", "Genesis"),
                      ("Yes/1972 - Close to the Edge", "Yes")):
        (a,) = albums_in(d)
        assert (a.albumartist, a.track_count, a.disc_count, a.inconsistent) == (artist, 3, 1, False)


def test_multi_disc_album_in_disc_subdirectories(corpus, root):
    scan(root)
    (a,) = albums_in("Gama Test Orchestra/Two Disc Set")
    assert (a.track_count, a.disc_count) == (4, 2)
    assert {os.path.basename(os.path.dirname(t.file.relpath)) for t in a.tracks.all()} == {"CD1", "CD2"}


def test_multi_disc_album_in_one_directory(corpus, root):
    scan(root)
    (a,) = albums_in("Gama Test Orchestra/Flat Double Album")
    assert (a.track_count, a.disc_count, a.inconsistent) == (4, 2, False)


def test_duplicate_titles_are_separate_albums(corpus, root):
    scan(root)
    hits = Album.objects.filter(album="Greatest Hits")
    assert hits.count() == 2
    assert {a.albumartist for a in hits} == {"Gama Duplicate A", "Gama Duplicate B"}


def test_editions_are_separate_albums(corpus, root):
    scan(root)
    eds = Album.objects.filter(album="Anniversary")
    assert eds.count() == 2
    assert {a.mb_albumid for a in eds} == {"11111111-1111-4111-8111-111111111111",
                                          "22222222-2222-4222-8222-222222222222"}


def test_inconsistent_directory_splits_and_is_flagged(corpus, root):
    scan(root)
    parts = albums_in("Gama Inconsistent/Mixed Bag")
    assert len(parts) == 2 and all(a.inconsistent for a in parts)
    by_artist = {a.albumartist: a for a in parts}
    assert by_artist["Gama Inconsistent"].track_count == 3 and by_artist["Someone Else"].track_count == 1
    summary = by_artist["Gama Inconsistent"].genres_summary
    assert summary["split_directory"] and not summary["consistent"]
    assert summary["union"] == ["Jazz", "Fusion"]


def test_compilation_without_albumartist_stays_one_album(corpus, root):
    d = corpus.root / "Various/Compilation"
    d.mkdir(parents=True)
    src = corpus.path(f"{KC}/01 21st Century Schizoid Man.flac")
    for n, artist in enumerate(["Artist One", "Artist Two", "Artist Three"], 1):
        p = d / f"{n:02d}.flac"
        shutil.copyfile(src, p)
        subprocess.run(["metaflac", "--remove-tag=ALBUMARTIST", "--remove-tag=ARTIST", "--remove-tag=ALBUM",
                        f"--set-tag=ARTIST={artist}", "--set-tag=ALBUM=Hits of Gama", str(p)], check=True)
    scan(root)
    (a,) = albums_in("Various/Compilation")
    assert (a.albumartist, a.track_count, a.inconsistent) == ("", 3, False)


def test_unreadable_files_indexed_with_read_ok_false(corpus, root):
    scan(root)
    for rel in ("Broken/garbage.flac", "Broken/zero_bytes.mp3", "Broken/truncated.m4a", "Format Zoo/unsupported.wav"):
        mf = MediaFile.objects.get(relpath=rel)
        assert not mf.read_ok and mf.read_error and mf.writable_reason
        assert not Track.objects.filter(file=mf).exists()
    skipped = MediaFile.objects.get(relpath="Format Zoo/flac_mixed_style.flac")
    assert skipped.read_ok and skipped.writable_reason == "mixed-genre-style"


def test_track_fields_and_genres(corpus, root):
    scan(root)
    t = Track.objects.get(file__relpath="Yes/1972 - Close to the Edge/01 Close to the Edge.m4a")
    assert (t.title, t.albumartist, t.album_title) == ("Close to the Edge", "Yes", "Close to the Edge")
    assert t.genres == ["Rock;Progressive Rock"] and t.genre_style == "delimited"


def test_temp_files_ignored(corpus, root):
    (corpus.root / KC / ".gama-tmp-job-0-abc.flac").write_bytes(b"partial")
    scan(root)
    assert not MediaFile.objects.filter(relpath__contains=".gama-tmp-").exists()


def test_symlinked_directories_are_not_followed(corpus, root, tmp_path):
    outside = tmp_path / "outside"
    shutil.copytree(corpus.root / KC, outside)
    (corpus.root / "link").symlink_to(outside)
    scan(root)
    assert not MediaFile.objects.filter(relpath__startswith="link/").exists()


# --------------------------------------------------------------------------- rescans

def test_rescan_updates_only_the_changed_file(corpus, root):
    scan(root)
    before = {mf.relpath: mf.scanned_at for mf in MediaFile.objects.all()}
    album_ids = {a.identity_key: a.id for a in Album.objects.all()}
    target = f"{KC}/02 I Talk to the Wind.flac"
    subprocess.run(["metaflac", "--remove-tag=GENRE", "--set-tag=GENRE=Jazz", str(corpus.path(target))], check=True)
    s = scan(root)
    assert (s.added, s.updated, s.removed, s.unchanged) == (0, 1, 0, len(before) - 1)
    after = {mf.relpath: mf.scanned_at for mf in MediaFile.objects.all()}
    assert [r for r in after if after[r] != before[r]] == [target]
    assert Track.objects.get(file__relpath=target).genres == ["Jazz"]
    assert {a.identity_key: a.id for a in Album.objects.all()} == album_ids  # ids stable
    kc = Album.objects.get(album_dir=KC)
    assert kc.inconsistent and not kc.genres_summary["consistent"]


def test_rescan_removes_vanished_files_and_empty_albums(corpus, root):
    scan(root)
    for p in sorted((corpus.root / "Yes").rglob("*.m4a")):
        p.unlink()
    s = scan(root)
    assert s.removed == 3
    assert not Album.objects.filter(album_dir__startswith="Yes/").exists()


def test_rescan_with_child_process_reader(corpus, root):
    s = scan_root(root)  # default reader: readproc children
    assert s.added == len(corpus.manifest)
    assert Album.objects.filter(album_dir=KC).count() == 1
