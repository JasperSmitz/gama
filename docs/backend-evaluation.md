# Gama: backend technology evaluation

*2026-11-17. Research only; Gama itself has not been started.*
Evidence lives in [`research/backend-eval/`](../research/backend-eval/): the spec, fixture builder, neutral verifier, the three disposable prototypes and per-candidate reports.

## Recommendation

**Python + Django + mutagen.**

The highest-priority criterion is safe, correct metadata editing. On that criterion mutagen was the only library that:

- wrote all five formats correctly with its defaults;
- needed no workarounds;
- did not need a fork.

The failures we found with mutagen are documented normalisation behaviours. Each one can be switched off with an existing option.

lofty (Rust) got to the same verifier result, but only after five fixes for silent field changes. Those fixes were found in a one-afternoon test on 15 files.

go-taglib (Go) makes changes to files that cannot be turned off through its API:

- it upper-cases every Vorbis comment key;
- it appends a lossy ID3v1 tag to every MP3 it saves.

Django then covers the application concerns with the least custom code: authentication, sessions, CSRF, the ORM, migrations, the admin and a tasks API.

Rust is the runner-up. If deployment footprint or licence weighed more than metadata correctness, Rust + Axum + lofty would win. Choosing it means owning the lofty workarounds and upstreaming fixes for them. Go is not recommended while its only maintained all-format writer is go-taglib.

## Comparison

| | Python + Django + mutagen | Go + go-taglib | Rust + Axum + lofty |
|---|---|---|---|
| Library | mutagen 1.48.1, pure Python, GPL-2.0+ | go.senan.xyz/taglib 0.15.0 (TagLib 2.1.1 as WASM), LGPL-2.1 | lofty 0.25.5, MIT/Apache-2.0 |
| All 5 formats read/write | ✅ | ✅ | ✅ |
| Correct by default | ✅ except documented normalisation (ID3v1 merge, ID3v2.3→2.4) | ❌ Vorbis keys upper-cased and reordered; ID3v1 appended to every MP3 | ❌ 4 silent bugs + 1 harmful default (see below) |
| Native multi-value genres | Vorbis repeated fields, ID3v2.4 NUL-separated, MP4 multi-`data` | same | same, but MP4 needs care |
| Pictures preserved (incl. 2-picture FLAC) | ✅ | ✅ | ✅ |
| Audio data changed | never | once: an appended ID3v1 tag after a truncated frame changed the decoded packets | never |
| Content-based format detection | ❌ extension-biased (needs a sniff fallback) | ❌ extension only, no override | ✅ |
| Write to temp path / file handle | ✅ path or file object | path only (the extension decides the parser) | ✅ any `File` |
| Malformed input | no crashes; some non-`MutagenError` exceptions | no crashes; some failures return `nil` error | no panics seen; open stack-overflow and hang issues |
| Read 34 fixtures | 75 ms / 30 MB | 230 ms / 24 MB warm | 10 ms / 14 MB |
| Deployment | interpreter + venv (uv); no native libraries | one static 7 MB binary | one 2–3 MB binary, libc only |
| Maintenance | very mature (Picard, beets, Quod Libet); slow, bursty releases | young, 0.x, single maintainer; bundled TagLib 5 releases behind | active, 0.x with breaking minors; single main maintainer |
| App stack | Django: auth, sessions, ORM, migrations, admin, tasks API built in | `net/http`/chi, goose, sqlc, scs: assembled from parts | axum, rusqlite + migrations, tower-sessions: assembled, more code |

## Metadata experiments

### Fixtures

- **Real files** copied from `/storage/music`:
  - 3 FLACs with repeated `GENRE` fields and no padding;
  - 3 ID3v2.4 MP3s with lyrics, cover art and an ID3v1 trailer;
  - 3 AAC M4As with moov after mdat.
- **Generated** Ogg Vorbis and Opus albums with cover art, MusicBrainz, ReplayGain and Unicode custom tags.
- **14 edge files**:
  - ID3v2.3, APEv2 + ID3v1, a two-picture FLAC, a FLAC with an ID3v2 prefix;
  - ALAC, a faststart M4A;
  - truncated, header-only, zero-byte, garbage and misnamed files.
- **A mixed partial-failure album.**

Every candidate worked on its own disposable copy. `/storage/music` was only read.

### Neutral verifier

[`harness/verify.py`](../research/backend-eval/harness/verify.py) uses only ffmpeg, ffprobe, metaflac and raw-byte parsers. It never uses a candidate library. It checks:

- decoded PCM md5 and compressed-packet md5;
- picture bytes, type and description;
- every non-target tag;
- a raw ID3v2 frame inventory;
- raw Vorbis comments, through metaflac and an Ogg packet parser;
- raw MP4 `ilst`;
- presence and changes of ID3v1 and APEv2;
- file mode;
- leftover temp files.

**The library's own re-read is not a sufficient verifier.** All three libraries normalise on read, so they cannot see the changes they make themselves. Every lofty bug and the mutagen ID3v1 promotion were invisible to the prototype's own post-write check. Gama should ship an independent verifier like this one as a test oracle.

The first version of the verifier also had blind spots: ffprobe shows only the first ID3v2.4 value, and it hides ID3v1. A second-opinion review found six further gaps, all now closed. The figures below come from the final verifier.

### Results (final verifier)

| Experiment | Python | Go | Rust |
|---|---|---|---|
| E1 read 34 files | 29 ok, 5 clean errors. `actually_flac.mp3` needed the prototype's content sniff | 28 ok, 6 errors. `actually_flac.mp3` misread as an empty MPEG file | 29 ok, 5 clean errors, content detection correct |
| E2 genres on 5 albums (15 files) | 15/15 ✅ | 6/15. MP3 and M4A ✅. All 9 FLAC/Ogg/Opus fail: keys upper-cased (`encoder`→`ENCODER`), FLAC `GENRE` moved | 15/15 ✅ *(with workarounds)* |
| E3 field round trip (15 files) | 14/15. `mp3_id3v1_only`: v1 genre promoted into v2 | 12/14. `mp3_id3v23` and `truncated.mp3`: ID3v1 tag *added*; on the truncated file the audio packet md5 changed | 15/15 ✅ *(with workarounds)* |
| E4 genres on edge files | 9/10. Same v1-promotion file | 4/9. 3 FLAC key case/order failures, 2 MP3s with an ID3v1 added | 10/10 ✅ |
| E5 partial failure, rollback | ✅ a–d | ✅ a–d, plus SIGKILL recovery | ✅ a–d |
| True ID3v1-only MP3 (extra test) | creates ID3v2.4 and copies the v1 fields into it | same | refused by the prototype's own check; original untouched |

### Behaviour per library

**mutagen**

- **Multi-value genres.** Writes repeated `GENRE=` fields, an ID3v2.4 `TCON` with NUL separators, and an MP4 `©gen` with several `data` atoms.
- **Byte preservation.** Keeps Vorbis key case and the order of unrelated comments.
- **ID3 normalisation.** On load it translates ID3v2.3 to v2.4 (`TYER`+`TDAT` become `TDRC`). It also merges ID3v1 into v2 (`load_v1=True`), which is how v1 fields get promoted on save. An existing ID3v1 tag is re-synced from the new values.
- **Keeping v2.3** requires `update_to_v23()` and `save(v2_version=3)`. Without `update_to_v23()`, it writes an invalid `TDRC` frame into a v2.3 tag.
- **Preserved:** an APEv2 trailer. It is invisible to the `MP3` class, so `mutagen.apev2` has to read it separately.
- **ID3v2 prefix on FLAC:** kept but left stale.
- **Exceptions:** fuzzing turned up `IndexError`/`ValueError` on corrupt Ogg, so callers must catch `Exception`.

**go-taglib (TagLib 2.1.1)**

- **Vorbis comments:** the block is rewritten with upper-cased keys, and the edited key moves to the end.
- **ID3v1 on every MP3 save:** `MPEG::File::save` defaults to AllTags + Duplicate, so every save appends an ID3v1 tag. Non-Latin-1 text becomes empty in it. On a truncated file that tag changed the decoded audio packets.
- **ID3v2.3 is always upgraded to 2.4.** There are no save options, and the API is path-only.
- **Error handling:** `ReadProperties` returns `nil` error for unreadable files, and a zero-byte file counts as valid MPEG.
- **No timeouts:** WASM calls cannot be cancelled.
- **Fixing it** means a custom TagLib/WASM build. The cgo route with system TagLib 2.3.2 showed the same behaviour.

**lofty 0.25.5.** These silently changed unrelated data until the Rust prototype worked around them:

1. `implicit_conversions` (on by default) splits `TRACKNUMBER=1/24` into two fields.
2. MP4 1-byte integer atoms are rewritten as 4 bytes. ffmpeg then reads `stik` 1 as 0.
3. `VorbisComments::remove` reorders unrelated comments.
4. `set_date` on ID3v2 adds a second `TDRC`, so readers keep the old year.
5. FLAC files with an ID3v2 prefix cannot be saved.

The generic `Tag` API drops unmapped items. Lossless writes need the per-format types. lofty also drops the optional trailing NUL from `USLT`, which is harmless. Open issues: stack overflow (#784, which `catch_unwind` cannot catch), an AAC hang (#783), and an APE tag overwritten as ID3v1 (#762).

### Safe bulk editing: lessons for Gama, independent of language

All three prototypes implemented the same protocol: preflight, backup, journal, temp copy in the same directory, library write, fsync, reread, atomic rename, rollback. It worked in every planned E5 scenario. The review still found real gaps:

- **Backup collisions.** Reusing a backup directory overwrote the only copy of an original (Python, Rust). Use content-addressed, `O_EXCL` backups.
- **Unverified restore.** Rollback replaced files before it checked the backup hash (Python, Rust). Verify the backup first.
- **Post-rename failures.** A failure after the rename left a modified file that rollback skipped (all three). Journal a `renaming` state, and roll back by comparing hashes, not by state.
- **Clobbered edits.** `rollback` would overwrite later edits. Restore only when the current hash equals `sha256_after`. Otherwise report a conflict.
- **Read-only files.** `rename()` over a 0444 file succeeds, so an explicit write-permission check is the only guard.
- **Concurrent changes.** Re-hash the original immediately before the rename. Make the temp copy from the verified backup.
- **What rename loses.** Rename creates a new inode, which loses owner, xattrs, ACLs and hard links. Preserve what can be preserved, and document the rest.
- **Isolation.** Run parsing and writing in killable worker processes with timeouts. Python threads cannot be killed, and Rust stack overflows abort the process.

## Application development

| Concern | Python + Django | Go | Rust |
|---|---|---|---|
| HTTP API | Django + Django Ninja (typed schemas, OpenAPI) | `net/http` + chi | axum + tower-http |
| SQLite + migrations | Django ORM + built-in migrations; SQLite well supported (WAL via settings) | goose + sqlc; ncruces/go-sqlite3 (WASM) or mattn (cgo) | rusqlite + rusqlite_migration, or sqlx |
| Scanning / watching | `os.scandir` + watchfiles; process pool for parsing | WalkDir + fsnotify (not recursive) | walkdir/jwalk + notify |
| Background jobs | Django Tasks API; `manage.py` worker with a DB-backed queue (`django-tasks`); a separate process gives isolation for free | goroutines + SQLite job table | tokio tasks + SQLite job table |
| Auth | built-in users, sessions, CSRF, password hashing (argon2) | scs + x/crypto/argon2, assembled | tower-sessions + argon2, assembled |
| Testing | pytest + hypothesis | `testing` + testscript | `cargo test` + insta |
| Development speed | fastest; least code; no build step | fast; more glue | slowest; most code; strongest compile-time checks |

## Deployment

- **Python.**
  - Pin the interpreter with `uv` (managed Python plus a lockfile). Otherwise Arch's rolling Python upgrades break venvs, while Debian 13 ships an older Python.
  - Ship as a uv-built venv with a systemd unit, a PKGBUILD or `.deb`, or an OCI image.
  - The footprint is fine for a desktop or home server: tens of MB for the scanner, and roughly 100 MB for a Django process (an estimate; not measured).
- **Go and Rust.** A single binary, the simplest option on both Arch and Debian.
- **Library portability.** This applies to every candidate. Store paths relative to a configured library root, never as absolute paths. Keep the database, backups and caches in a separate data directory, not inside `/storage/music`. Moving the library then means changing one setting and rescanning, with files matched by relative path.

## Risks

1. **GPL.** mutagen is GPL-2.0+. Gama is MIT. MIT code may import GPL code, but a *distributed* Gama including mutagen must be distributed under GPL terms. This does not matter for personal self-hosting. Decide it before any public release: relicense Gama to GPL-3.0-or-later, or accept the combined-work terms.
2. **mutagen release cadence.** There was a three-year gap before 1.48. An Ogg fix sits unreleased. Pin the version, vendor patches if needed, and run the fixture suite on every upgrade.
3. **Normalisation policy.** ID3v2.3→2.4 upgrade, ID3v1 handling, APEv2, genre separators in single strings (the real library has `Rock;Progressive Rock` in one M4A atom), and MP4 multi-`data` genres. Many players show only the first of those `data` values. These need explicit, tested Gama policies, not library defaults.
4. **Fixture coverage is small.** 35 files. Before Gama writes to `/storage/music`, run read-only scans over the whole library, and run write tests on copies of a larger sample.
5. **Python performance.** Parsing took about 0.5 ms per file, so a 100k-track rescan takes about a minute on one core. Use a process pool and incremental scans (mtime + size).

## Initial repository architecture

The full repository layout and component responsibilities are in [architecture.md](architecture.md).
