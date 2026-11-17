# Gama backend

Django project for Gama's bulk genre editing. The design is in
[docs/plan/bulk-genre-editing.md](../docs/plan/bulk-genre-editing.md).

## Setup

You need Python 3.14, plus `ffmpeg`, `ffprobe` and `metaflac` on `PATH` for the
test fixtures and the oracle.

```sh
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # requirements.txt alone for production
```

All dependencies are pinned exactly, including transitive ones. **Arch
upgrades the system Python**, which breaks the venv. To rebuild it, delete
`backend/.venv` and run the two commands above again.

## Configuration

Gama reads `$GAMA_CONFIG` (or `$XDG_CONFIG_HOME/gama/gama.toml` if that file
exists). Environment variables override the file:

| Key / variable | Default | Meaning |
|---|---|---|
| `library_roots` / `GAMA_LIBRARY_ROOTS` (`:`-separated) | none | Absolute paths of the music collections |
| `data_dir` / `GAMA_DATA_DIR` | `$XDG_DATA_HOME/gama` | Database, backups and journals |
| `backup_margin_mb` / `GAMA_BACKUP_MARGIN_MB` | `64` | Free-space margin for backups |
| `verify_audio` / `GAMA_VERIFY_AUDIO` | `auto` | `auto` or `off` |

Startup refuses a `data_dir` inside a library root, a library root inside
`data_dir`, and overlapping roots.

Django settings come from the environment:

- `GAMA_DEBUG=1`: development mode.
- `GAMA_SECRET_KEY`: required when `GAMA_DEBUG` is not `1`; must be at least 50 characters (e.g. `python -c "import secrets; print(secrets.token_urlsafe(50))"`).
- `GAMA_ALLOWED_HOSTS`: comma-separated hosts, in addition to localhost.
- `GAMA_SECURE_COOKIES=1`: use this behind TLS.

SQLite runs with `journal_mode=WAL` and `synchronous=FULL`.

## Run (development)

```sh
export GAMA_DEBUG=1 GAMA_DATA_DIR=/tmp/gama-dev   # never inside a music root
mkdir -p "$GAMA_DATA_DIR"
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Every page requires login. Only the login and logout pages are exempt.

## Tests

```sh
cd backend
.venv/bin/python -m pytest -q
```

- Tests use `gama.settings_test`. It sets no library roots and puts the data
  dir in the system temp dir.
- `tests/conftest.py` aborts the session if any configured root or the data dir
  resolves to or under `/storage/music`.
- Every audio file in the tests is synthetic and lives under pytest's
  `tmp_path`.

Layout:

- `tests/unit`: configuration and the production-path guard.
- `tests/web`: login required on every URL (the test iterates the URLconf),
  login/logout and SQLite pragmas.
- `tests/corpus`: the inventory of the fixture corpus.
- `tests/conformance`: oracle self-tests (planted defects).

### Fixture corpus (`tools/fixtures/build.py`)

The builder creates a fully synthetic library of 59 files in about a second.

- **Audio** comes from ffmpeg `lavfi` (sine + noise). **Covers** come from
  `lavfi color`.
- **Tags** are written by `metaflac` (FLAC) or by raw-byte writers in the
  builder: ID3v2.2/2.3/2.4, ID3v1, APEv2, Ogg comment packets (re-paged, with
  CRCs) and MP4 `ilst` atoms. mutagen is not used.
- **Contents:**
  - albums: the three example albums, a multi-disc album both with `CD1/CD2`
    subdirectories and flat with disc tags, a duplicate title by two artists,
    two editions with different MusicBrainz ids, and an inconsistent album;
  - format edge cases: multi-field, delimited and mixed genres, two pictures
    without padding, an ID3-prefixed FLAC, a full ID3v2.4 file (UFID, POPM,
    USLT, TXXX, two APICs, APEv2, ID3v1), ID3v2.3 slash and numeric genres,
    ID3v2.2, a true ID3v1-only MP3, an untagged MP3, MP4 moov-after-mdat,
    faststart, ALAC, multi-`data`, `gnre`, freeform and fragmented files,
    Ogg Vorbis, Opus and WAV;
  - broken files.

A session fixture builds the corpus once. The `corpus` fixture gives each test
its own writable copy (`corpus.root`) and a pristine master (`corpus.master`).
To build it by hand: `python tools/fixtures/build.py /tmp/somewhere`.

### Oracle (`tools/verify/verify.py`)

The oracle is independent of mutagen. It uses stdlib parsers plus ffmpeg audio
checksums.

- `compare(master, work, mode, expected_genres) -> problems` checks one file.
- `compare_tree(master_root, work_root, {relpath: genres})` checks a whole
  library. Unlisted files must be byte-identical, and extra files (leftover
  temp files) count as problems.

The module docstring lists every check. Compared with the research verifier, it
closes V1–V8 and covers V9 for MP3 (the raw audio region) and MP4 (raw `moov`
boxes, including edit lists).

## Milestones

- **M1 (scaffold, fixtures, oracle): done.**
  - Django project with config, auth middleware, login page, empty home page
    and admin.
  - Fully synthetic corpus and ported oracle with self-tests.
  - The fixture builder and the oracle are stdlib-only by design.
- **M2 (metadata read path): done.**
  - `gama.metadata` detects formats from their content, reads genres exactly as stored, and decides which files may
    be written. Each file gets a snapshot of everything except the genre.
  - mutagen runs only in child processes with timeouts (`readproc`).
  - Raw-byte checks refuse files that mutagen would rewrite with data lost: non-UTF-8 text, dropped or merged ID3
    frames, a lossy ID3v2.3 upgrade, and MP4 genre atoms it cannot parse.
- **M3 (genre operations and planner): done.**
  - `genres.apply` implements add, remove and replace. Validation is in `genres.validate_request`.
  - `planning.plan_files` reads each file fresh, fingerprints it, and builds a plan with a digest.
  - `genres.py` has 100 % branch coverage, enforced by `tests/planning/test_coverage.py`.
- **M4 (format writers): done.**
  - `formats/*.apply_genres` writes the planned stored values and nothing else:
    - Vorbis: GENRE fields in place, keeping key spelling and padding length;
    - ID3: v2.4 TCON; a 2.3 tag is upgraded only when that is lossless; the ID3v1 trailer is restored
      byte-for-byte; APEv2 is untouched;
    - MP4: `©gen` data atoms; a numeric `gnre` atom is replaced by `©gen` with the same name.
  - `safewrite.mutate` runs the write in a child process with a timeout. It refuses unless the temp file is still
    in the planned state.
  - `safewrite.verify` re-reads the backup and the temp file in a child and compares:
    - genres, exactly;
    - the snapshot, allowing only the listed 2.3→2.4 conversions;
    - the raw ID3v1/APEv2 bytes;
    - the ffmpeg audio packet md5.
  - `tests/conformance/test_writers.py` runs every writable corpus file × add/remove/replace through the oracle and
    the verifier.
- **M5 (safe write engine): done.** The engine lives in `gama.safewrite` and imports no Django.
  - Backups (`backups.py`): content-addressed (`<data_dir>/backups/sha256/ab/<sha>`, read-only). They are written with
    `O_EXCL`, re-hashed, and never overwritten or deleted.
  - Journal (`journal.py`): one `O_EXCL` JSONL file per job in `<data_dir>/journal/`, fsynced after every record.
  - `preflight.py` checks each file and the free space.
  - `writer.run_apply` runs Phase A (back up everything, then verify every backup) and Phase B (per file: temp copy
    from the backup, mutate child, verify, attributes, re-hash, rename, re-hash). Batch policy: stop-and-rollback, or
    continue.
  - `restore.run_restore` and `recovery.recover` handle restores and crash recovery. Outcomes are decided by hash,
    never by the journaled state alone.
  - A global `flock` allows one write job at a time.
  - Failpoints (`fsutil.failpoint`) are used by the crash tests, which run the engine in a real subprocess and end it
    with `os._exit`.
  - **What is atomic:** each file is replaced atomically by rename(2). A multi-file job is not atomic; recovery and
    restore undo partial jobs. Durability depends on the filesystem honouring fsync.

### Known, accepted limitations
- **Frames hidden after ID3 padding.** An ID3v2 frame placed after zero padding is invisible to mutagen and to Gama's
  raw frame walk (both stop at the padding), so a write drops it without notice. Tags written this way are malformed.
  The case is accepted rather than detected.
- **MP4 writes are refused when mutagen would re-render other items** (`mp4-items-normalised`): unknown cover types,
  non-zero locales, or duplicate atoms. Those files can be edited with another tool, or left as they are.
