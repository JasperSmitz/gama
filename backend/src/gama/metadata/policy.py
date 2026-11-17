"""Format policy: which files Gama may write, and what the preview must warn about.

See docs/plan/bulk-genre-editing.md section 1.4. Every rule here is tested.
"""
from __future__ import annotations

from gama.metadata import genres
from gama.metadata.detect import Detection
from gama.metadata.model import FileStat, TrackTags

# Skip reasons: the file is read and shown, but never written.
SKIP = {
    "unsupported-format": "Format is not supported for writing",
    "unreadable": "File could not be read (corrupt or truncated)",
    "extension-mismatch": "File content does not match its extension",
    "id3v2.2": "ID3v2.2 tags are not written",
    "id3v1-only": "MP3 has only an ID3v1 tag; writing would have to create an ID3v2 tag",
    "flac-id3-prefix": "FLAC file starts with a non-standard ID3v2 tag, which would be left stale",
    "mp4-fragmented": "Fragmented MP4 files are not written (mutagen #748)",
    "mp4-truncated": "MP4 atoms extend past the end of the file (truncated)",
    "mp4-gnre-and-gen": "MP4 has both a numeric gnre atom and a ©gen atom",
    "mixed-genre-style": "Genres mix separate values and ';'-joined values; refusing to guess",
    "irregular-delimiter": "';'-joined genre has irregular spacing that a rewrite would change",
    "symlink": "Symbolic links are not written",
    "hardlink": "File has several hard links; replacing it would split them",
    "not-owner": "File is not owned by the Gama user, so ownership cannot be preserved",
}

# Warnings: the file can be written; the preview must say this.
WARNINGS = {
    "no-id3-tag": "MP3 has no ID3 tag; a new ID3v2.4 tag containing only the genre will be created",
    "id3v2.3-upgrade": "ID3v2.3 tag will be saved as ID3v2.4 (TYER/TDAT become TDRC; other frames kept)",
    "id3v1-present": "ID3v1 trailer is kept byte-identical, so its genre may disagree with ID3v2",
    "apev2-genre": "APEv2 tag holds a Genre item, which Gama does not edit",
    "numeric-genre-reference": "Genre is stored as a numeric ID3 reference; a changed file stores the name",
    "mp4-gnre": "Numeric MP4 gnre atom will be replaced by a ©gen atom with the same name",
    "duplicate-genres": "Genres contain duplicates (case-insensitive); they are kept unless removed",
    "empty-genre-value": "Genres contain an empty value; it is kept unless replaced",
}


def classify(det: Detection, tags: TrackTags, st: FileStat, euid: int) -> tuple[str | None, list[str]]:
    """(skip reason or None, warnings) for a file whose tags were read."""
    warnings: list[str] = []
    g = tags.genre
    native = tags.native

    if det.format == "mp3":
        if native.get("id3_version") is None and not native.get("has_id3v1"):
            warnings.append("no-id3-tag")
        if native.get("id3_version") == "2.3":
            warnings.append("id3v2.3-upgrade")
        if native.get("has_id3v1") and native.get("id3_version") is not None:
            warnings.append("id3v1-present")
        if native.get("numeric_references"):
            warnings.append("numeric-genre-reference")
    if any(k.lower() == "genre" for k in native.get("ape_keys", [])):
        warnings.append("apev2-genre")
    if det.format == "mp4" and native.get("has_gnre"):
        warnings.append("mp4-gnre")
    if genres.has_duplicates(g):
        warnings.append("duplicate-genres")
    if genres.has_empty(g):
        warnings.append("empty-genre-value")

    skip = None
    if st.is_symlink:
        skip = "symlink"
    elif st.nlink > 1:
        skip = "hardlink"
    elif st.uid != euid:
        skip = "not-owner"
    elif not det.extension_ok:
        skip = "extension-mismatch"
    elif det.format == "flac" and det.id3_prefix:
        skip = "flac-id3-prefix"
    elif det.format == "mp3" and det.id3v2_version == 2:
        skip = "id3v2.2"
    elif det.format == "mp3" and native.get("id3_version") is None and native.get("has_id3v1"):
        skip = "id3v1-only"
    elif det.format == "mp4" and det.mp4.get("fragmented"):
        skip = "mp4-fragmented"
    elif det.format == "mp4" and det.mp4.get("truncated"):
        skip = "mp4-truncated"
    elif det.format == "mp4" and det.mp4.get("has_gnre") and det.mp4.get("has_gen"):
        skip = "mp4-gnre-and-gen"
    elif g.style == "mixed":
        skip = "mixed-genre-style"
    elif not genres.delimited_round_trips(g):
        skip = "irregular-delimiter"
    return skip, warnings


# Save options per format, used by the writers (M4).
ID3_SAVE = {"v2_version": 4, "v1": 1}   # v1=1 updates an existing trailer in place; the writer
                                        # then restores the original 128 bytes (Q2: byte-identical).
ID3_LOAD = {"load_v1": False, "translate": False}
