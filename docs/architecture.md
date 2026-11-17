# Gama: initial repository architecture

*Status: proposal, 2026-11-17. It follows from the [backend evaluation](backend-evaluation.md). Nothing here is implemented yet.*

## Principles

1. **Files are the source of truth for metadata.** The database is an index and a cache. It can always be rebuilt by rescanning.
2. **Writing to user files is a separate, guarded subsystem.** It is the only code allowed to open audio files for writing. Everything else reads.
3. **Never verify a write only with the library that made it.** An independent verifier runs in the test suite (ffmpeg, metaflac, raw parsers), and optionally in production.
4. **Format policies are explicit Gama decisions, not library defaults.** Examples: ID3 version, ID3v1 handling, genre separators.
5. **The metadata core has no Django imports.** That keeps it testable in isolation, and it can be replaced if mutagen ever has to be.
6. **Location independence.** Paths are stored relative to a configured library root. The data directory (database, backups, caches) lives outside the library.

## Layout

```
gama/
├── backend/                      Python ≥3.13, managed with uv (pyproject.toml + uv.lock)
│   ├── src/gama/
│   │   ├── settings.py, urls.py, asgi.py
│   │   ├── metadata/             PURE PYTHON, no Django: the mutagen boundary
│   │   │   ├── model.py          TrackTags: canonical fields, genres[], pictures[], extra{}
│   │   │   ├── detect.py         content sniffing (magic bytes) before extension
│   │   │   ├── formats/          id3.py, vorbis.py (FLAC/Ogg/Opus), mp4.py: read + apply changes
│   │   │   ├── policy.py         ID3 version, v1 load/update/never-add, APEv2, genre splitting
│   │   │   └── snapshot.py       canonical snapshot for before/after comparison
│   │   ├── safewrite/            PURE PYTHON, no Django: the only writer of user files
│   │   │   ├── backups.py        content-addressed (sha256) store, O_EXCL, retention
│   │   │   ├── journal.py        per-item state machine, fsync'd
│   │   │   ├── writer.py         preflight → backup → temp-from-backup → write → fsync
│   │   │   │                     → verify → re-hash original → rename → commit
│   │   │   ├── restore.py        verify backup hash first; restore only if current == sha_after
│   │   │   └── recovery.py       on startup, resolve non-terminal journal entries
│   │   ├── worker/               subprocess pool with timeouts for parsing and writing
│   │   │                         (isolates hangs and crashes from the web process)
│   │   ├── library/              Django app: LibraryRoot, MediaFile(root, relpath, size,
│   │   │                         mtime, sha?), Track, Album, Artist, Genre (M2M), Picture
│   │   │                         (hash-deduped), scanner (scandir diff), watcher (watchfiles)
│   │   ├── edits/                Django app: EditBatch / EditItem models; services that plan
│   │   │                         edits, enqueue them, and expose progress and rollback
│   │   ├── jobs/                 task definitions (scan, write batch, thumbnails);
│   │   │                         Django Tasks API + database-backed worker
│   │   ├── accounts/             Django auth: users, sessions, CSRF; argon2 hashing
│   │   └── api/                  Django Ninja routers + schemas (OpenAPI for the frontend)
│   └── tests/
│       ├── unit/                 metadata, policy, safewrite (fault injection, e.g. --fail-after)
│       ├── conformance/          fixture corpus × formats, checked by the neutral verifier
│       └── api/                  Django test client
├── frontend/                     web UI; framework decision deferred (talks only to /api)
├── tools/
│   ├── fixtures/                 fixture builder (from research/backend-eval/harness)
│   └── verify/                   neutral verifier (ffmpeg/metaflac/raw parsers)
├── deploy/
│   ├── systemd/                  gama-web.service, gama-worker.service
│   ├── arch/PKGBUILD
│   ├── debian/
│   └── container/                OCI image (optional)
├── docs/
│   ├── adr/                      0001-backend-python-django-mutagen.md, ...
│   ├── backend-evaluation.md
│   └── architecture.md
└── research/backend-eval/        this evaluation (disposable, kept for reference)
```

## Runtime

- **Processes.** Two processes share one SQLite database in WAL mode:
  - `gama-web`: Django ASGI under uvicorn. Serves the API and the built frontend. Reads the database. Enqueues jobs.
  - `gama-worker`: runs scan jobs and edit jobs. Spawns short-lived parser and writer subprocesses with timeouts.
- **Configuration.** All configuration lives in a TOML file plus environment overrides:
  - `library_roots` (initially `/storage/music`)
  - `data_dir` (database, backups, thumbnails, logs; e.g. `/var/lib/gama` or `$XDG_DATA_HOME/gama`)
  - backup retention
  - format policies
- **Scanning.** Each scan does a full walk, diffs the results against the database by `(root, relpath, size, mtime)`, and parses only the changed files. A watcher triggers incremental scans, and a periodic full scan backstops it, because inotify can overflow and misses network filesystems.

## Edit flow

1. **Plan.** The API receives an edit, for example "set genres of album X". It creates an `EditBatch` with one `EditItem` per file, holding the target values and `sha256_before`. The user can preview the result.
2. **Enqueue.** A write job is enqueued for the worker.
3. **Write.** The worker runs `safewrite` for each item:
   - checks preflight conditions (the file can be parsed and written to);
   - stores a content-addressed backup;
   - creates a temp copy *from the backup* in the same directory, and lets mutagen edit it;
   - fsyncs it, then re-reads it with the snapshot comparison;
   - re-hashes the original to detect concurrent changes;
   - renames the temp copy over the original and fsyncs the directory;
   - journals each step (`backed_up → written → verified → renaming → committed`).
4. **Batch policy.** All-or-nothing by default, with rollback by hash comparison. Continue-on-error is opt-in, and each failure is reported.
5. **Undo.** A batch can be rolled back later. A file is restored only if its current hash still equals `sha256_after`. Otherwise Gama reports a conflict.
6. **Re-index.** Changed files are rescanned.

## First milestones (after this evaluation)

1. Scaffold `backend/` (uv, Django, Ninja, pytest) and promote the fixture builder and verifier into `tools/` and `tests/conformance`.
2. `metadata` read path + `detect` + `policy`, plus a **read-only** scan of the whole `/storage/music` to find unusual files before any write code exists.
3. `safewrite`, with fault-injection tests that cover the review findings (S1–S7).
4. Library models, scanner and API for browse, search and filter.
5. Frontend.
