# Gama: initial repository architecture

*Status: proposal, 2026-11-17, from the [backend evaluation](backend-evaluation.md). The bulk genre editing feature
is now built. Where it differs from this proposal, the "As built" section below is authoritative; the proposal is kept
for reference.*

## Principles

1. **Files are the source of truth for metadata.** The database is an index and a cache. It can always be rebuilt by rescanning.
2. **Writing to user files is a separate, guarded subsystem.** It is the only code allowed to open audio files for writing. Everything else reads.
3. **Never verify a write only with the library that made it.** An independent verifier runs in the test suite (ffmpeg, metaflac, raw parsers), and optionally in production.
4. **Format policies are explicit Gama decisions, not library defaults.** Examples: ID3 version, ID3v1 handling, genre separators.
5. **The metadata core has no Django imports.** That keeps it testable in isolation, and it can be replaced if mutagen ever has to be.
6. **Location independence.** Paths are stored relative to a configured library root. The data directory (database, backups, caches) lives outside the library.

## As built: bulk genre editing (2026-11-17)

Implemented per [plan/bulk-genre-editing.md](plan/bulk-genre-editing.md).
- Decisions: [ADR 0002](adr/0002-genre-editing-policies.md).
- How to use it and how to recover: [user guide](user-guide/genre-editing.md).

### Deviations from the proposal below

| Proposal | As built | Why |
|---|---|---|
| uv + uv.lock | `python -m venv` + `pip install -r requirements*.txt`, everything pinned exactly | uv is not installed. An Arch Python upgrade needs a venv rebuild (backend/README) |
| Django Ninja API + separate `frontend/` | Server-rendered Django templates, no JavaScript (progress pages use a meta refresh) | No npm, so no build step, and fewer moving parts |
| `jobs/` (Django Tasks) + `worker/` pool | One `EditJob` table + `manage.py gama_worker`, a single process guarded by a lifetime `worker.lock`. mutagen runs only in short-lived child processes with timeouts: `metadata/readproc.py` for reads, `safewrite/mutate.py` for writes | One job type does not justify a task framework. A DB row is inspectable and survives restarts |
| `accounts/` app, argon2 | `django.contrib.auth` + `LoginRequiredMiddleware` + the permission `edits.apply_edits` (`gama_grant`). Plans and jobs are personal (staff see all) | No extra dependency |
| (none) | `planning/`: a framework-free planner (fresh read, hash before and after the read, digest) | "Change planning and preview" is a separate responsibility in the requirements |
| `library/` with Artist, Genre M2M, Picture, watcher | LibraryRoot, MediaFile (incl. `album_dir`), Album (identity key), Track. A scandir scanner (`gama_scan`) without a watcher | Only what genre editing needs |
| `EditBatch` / `EditItem` | `EditPlan` / `PlanItem` (the digested preview) and `EditJob` / `JobItem` (outcome, read-back genres, verification, rollback outcome); `Backup` | A plan is confirmed once; a job mirrors its journal |
| Journal states `backed_up → written → verified → renaming → committed` | `planned → backed_up → temp_created → temp_written → verified → renaming → committed`, or failed / conflict / skipped / missing. Rollback outcomes go in a separate `restore_state` | Recovery and the UI need the finer states |
| Recovery: "resolve non-terminal entries on startup" | Runs at worker start **and before every job**. It decides by **current hash**. It **defers** ("recovery pending") when a library root is absent or is not the filesystem the job wrote to (the root's `st_dev`/`st_ino` is in the journal header), or a file cannot be read. New jobs touching those files are refused. An operator can resolve a pending job explicitly: `manage.py gama_recover --resolve JOB [--confirm-missing]`. A damaged journal blocks only the files it names | "Never guess" after a crash, and an unmounted disk must not turn edited files into "failed" |
| Undo via API | A restore preview (restore / nothing to do / conflict, optional force that backs up the current version), confirmed and run as a job by the worker. Also `manage.py gama_restore JOB` from the journal alone, when the UI or DB is down | Restores need the same review and safety as edits |

### Commands

- `gama_scan [--root] [--allow-empty]`: index the configured roots.
- `gama_demo_library DIR`: build a synthetic library.
- `gama_worker [--once]`: the write worker.
- `gama_grant USER [--revoke]`: grant or revoke the apply permission.
- `gama_recover [--resolve JOB [--confirm-missing]]`: run recovery, or resolve a pending job.
- `gama_restore JOB [--items] [--force] [--dry-run]`: restore from the command line.

### As-built layout (backend/src/gama)

- `metadata/`, `planning/`, `safewrite/`: no Django.
- `library/` (index, selection basket).
- `edits/` (plans, jobs, history, restore).
- `tests/`: unit, conformance (oracle per format), safewrite (crash tests in real subprocesses), web, library, edits,
  and e2e (real `gama_worker --once` subprocesses).
- `tools/fixtures` (synthetic corpus) and `tools/verify` (oracle).

## Layout (original proposal)

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
