# Plan: bulk genre editing

*Architect, 2026-11-17. Implements [requirements/bulk-genre-editing.md](../requirements/bulk-genre-editing.md) on the stack chosen in [backend-evaluation.md](../backend-evaluation.md) and the layout in [architecture.md](../architecture.md). Safety bugs S1–S10 and verifier gaps V1–V9 refer to [research/backend-eval/results/REVIEW.md](../../research/backend-eval/results/REVIEW.md).*

**Scope:** add, remove and replace genres on the tracks of selected albums. There is no other tag editing, no film support, no watcher, and no external lookups.

---

## 1. Key design decisions

### 1.1 Deviations from architecture.md (environment-driven)

| architecture.md | This plan | Why |
|---|---|---|
| uv + uv.lock | `python -m venv` + `pip install -r requirements.txt` (exact `==` pins, hashes optional) | uv is not installed. System Python is 3.14. An Arch Python upgrade needs a venv rebuild, documented in `backend/README.md` |
| Django Ninja API + separate `frontend/` | Server-rendered Django templates. No JS by default (progress page uses `<meta http-equiv="refresh">`). A vendored `htmx.min.js` is allowed later but not required | No npm, so no build step. Fewer moving parts |
| `jobs/` with Django Tasks + `worker/` pool | One `EditJob` table + `manage.py gama_worker` loop. Isolation comes from short-lived **child processes for mutagen calls only** (see 1.8) | One job type does not justify a task framework dependency. A DB row is inspectable and survives restarts |
| `accounts/` app, argon2 | `django.contrib.auth` views + `LoginRequiredMiddleware`, default PBKDF2 hasher | No extra native dependency. argon2 can be added later |
| (none) | New pure-Python package `gama/planning/` | The requirements name "change planning and preview" as a separate, framework-independent responsibility |

Pins (Developer verifies the exact latest patch at M1, all must support Python 3.14): `Django==6.0.*` (pinned exactly), `mutagen==1.48.1`, `pytest`, `pytest-django`. Nothing else at runtime.

### 1.2 Album identity

- **Album directory.** This is the parent directory of the track. If that directory's name matches `^(cd|disc|disk)[\s_-]*\d+$` (case-insensitive), the grandparent is used instead. That way `Album/CD1/` and `Album/CD2/` form one album.
- **Identity key.** `sha1(root_id, album_dir_relpath, norm(albumartist), norm(album), mb_albumid or "")`. `norm` is NFC + trim + whitespace collapse + `casefold`. If `albumartist` is missing, `""` is used, not the track artist, so compilations without albumartist stay one album.
- **Consequences.**
  - Duplicate titles in different directories or by different album artists are different albums.
  - Two editions are different albums, because they sit in different directories or have different MusicBrainz release ids.
  - Multi-disc albums are one album, whether the discs share a directory with `discnumber` tags or use disc subdirectories.
  - Inconsistent `albumartist`/`album` tags inside one directory split it into several albums. The selection list shows this; nothing is silently merged.
- **Display.** The selection UI shows the album, album artist, directory relpath, disc count, track count, formats and MB release id. Users can then tell look-alike albums apart.
- **Plans are bound to files, not to album ids.** A plan stores the exact `(root, relpath)` list resolved at preview time. Album rows are a derived index (rebuilt by scans), so they are only a selection aid.

### 1.3 Genre representation (pure, `metadata/genres.py`)

On read, every file gives a `GenreField`:

```
values:    list[str]   # exactly as stored, in order (one entry per Vorbis field / TCON value / ©gen data atom)
style:     "native" | "delimited" | "mixed" | "empty"
delimiter: str | None  # e.g. ";" or "; " (spacing preserved) when style == "delimited"
components: list[str]  # what the user sees as "the genres"
```

- **Delimited style.** A value is *delimited* when there is exactly one stored value and it contains `;`. Example: MP4 `©gen = "Rock;Progressive Rock"`, as found in the real library. `components` is the split, trimmed list. On write the value is joined again with the **same delimiter**, so the file keeps its convention. Nothing is flattened, and nothing is converted to multi-atoms unless the user asked for it.
- **Native style.** Each stored value is one component. Writes use the format's native multi-value form:
  - Vorbis (FLAC/Ogg/Opus): repeated fields. The existing key spelling (e.g. `GENRE`/`genre`) is reused, and `GENRE` is used if no field exists yet.
  - ID3v2.4: one `TCON` with NUL-separated values, `encoding=3`.
  - MP4: one `©gen` with several `data` atoms. An MP4 `gnre` (numeric) atom is read as its name. On write, the result must contain only `©gen`, with `gnre` removed and its value preserved by name. M2 adds a fixture test to pin mutagen's actual behaviour here.
- **Mixed style.** Several values where at least one contains `;`. The file is marked *problematic* and **excluded** from the write set with a warning. This is rare, and guessing would risk destroying information.
- **`/` and `,` are never separators** (`AC/DC`, `R&B/Soul`). ID3v2.3 `TCON "Rock/Post-Punk"` is one genre.
- **ID3 numeric references** (`(17)`, `17`) are resolved to names by mutagen's `TCON.genres`. If a changed file will store the name instead of the reference, the preview warns about it.

**Comparison and operations** (on `components`, `key(g) = NFC(collapse(trim(g))).casefold()`):

| Op | Rule | No-op when |
|---|---|---|
| Add `G` | Append `G` (as typed, trimmed) at the end, unless some component has the same key. Existing spelling wins: adding `rock` to `Rock` changes nothing and the preview notes the case difference | key already present |
| Remove `G` | Remove **every** component with that key, including case variants and duplicates. If nothing remains, delete the genre field entirely | key absent |
| Replace `[G1..Gn]` | Input is deduplicated by key (first spelling wins). The result is exactly that list. It is a separate operation with its own page and an extra confirmation checkbox | result `values` identical to stored `values` |

- **Unrelated components are kept as stored:** existing duplicates, odd case, empty values and order. Add and remove never "clean up" what was not requested. The preview warns about duplicates and empty values.
- **Input validation.** A genre may not be empty after trimming, may not contain `;`, NUL or control characters, and is at most 255 characters. At most 20 genres per replace.
- **A file "changes"** iff the new stored `values` list differs from the old one. Repeating the same operation on the result is always a no-op, which the property tests check.

### 1.4 Format policy (`metadata/policy.py`, explicit, all tested)

**Supported for writing:**

- FLAC
- MP3 with ID3v2.3 or v2.4
- MP4/M4A (AAC, ALAC, non-fragmented)
- Ogg Vorbis
- Opus

**Read and shown, but excluded from writing, with a warning that says why:**

- unknown formats (WAV, AIFF, WMA, APE, WavPack, …)
- unreadable or corrupt files
- content/extension mismatch (e.g. FLAC data in `.mp3`)
- ID3v2.2
- ID3v1-only MP3 (a write would have to create an ID3v2 tag)
- FLAC with a prepended ID3v2 tag (mutagen keeps it but leaves it stale)
- fragmented MP4 (mutagen #748)
- mixed genre style
- symlinks
- files with hard links (`st_nlink > 1`; a rename would split them)
- files not owned by the Gama process user (ownership cannot be preserved)

Untagged MP3 files (no ID3 at all) get a new ID3v2.4 tag that contains only `TCON`. The preview warns about this.

| Concern | Policy |
|---|---|
| ID3 load | `ID3(path, load_v1=False)` always. This prevents the ID3v1→v2 promotion found in E3/E4 |
| ID3 version | **v2.4 stays v2.4. v2.3 is upgraded to v2.4 on write**, which is mutagen's lossless default: `TYER`+`TDAT`→`TDRC`, all other frames kept. The preview shows a per-file warning. Keeping v2.3 would force multiple genres into one joined string that does not round-trip. → Open question Q1 |
| ID3v1 trailer | **Never created, never removed, kept byte-identical.** Save with `v1=1` (updates in place at the same offset), then rewrite the final 128 bytes with the original trailer. The v1 genre byte may then disagree with v2. The preview warns, and v2-aware readers ignore v1. → Q2 |
| APEv2 | Never edited. Bytes must be identical after the write (verified). If the APE tag holds a `Genre` item, the preview warns that it is not edited |
| Format detection | Magic-byte sniff first (`fLaC`, `ID3`/MPEG sync, `OggS` + `OpusHead`/`\x01vorbis`, `ftyp`). The mutagen class is chosen explicitly, never by `mutagen.File` scoring |
| Exceptions | Catch `Exception` around every mutagen call, not just `MutagenError`. Fuzzing produced `IndexError` and `ValueError` |

### 1.5 Plan / preview model and conflict detection

1. The selection (a session basket of album ids) plus the operation and genres produce an **EditPlan**.
   - The planner resolves album → files, **reads every file fresh from disk** (not from the index), fingerprints it and computes `genres_after`.
   - Each `PlanItem` gets an action: `change`, `nochange` or `skip` (unsupported or problematic, with a reason).
   - The planner also warns when an album directory contains audio files the index does not know about ("rescan suggested").
2. **Fingerprint** = `(size, mtime_ns, inode, sha256)` of the whole file. The size/mtime pair is shown, and sha256 is authoritative.
3. The preview page shows:
   - each album with its tracks;
   - before and after genres per track (stored values, with style shown);
   - counts of change / no change / skipped;
   - all warnings.

   The plan has a `digest` (sha256 of its canonical item list). The confirm form posts the plan id + digest. A plan can be confirmed **once** (one job per plan, enforced by a DB constraint).
4. **At apply, the worker preflight re-hashes every `change` item.** Any mismatch with the plan's `sha256_before` sets the job to `conflict` with zero files written. The UI then offers "Refresh preview", which builds a new plan for the same selection and operation and marks the old one `superseded`.
5. **During the write**, the original is re-hashed again just before the rename (S7). A late change fails that item as `conflict`, and the batch policy applies.

### 1.6 Safe write protocol (`safewrite/`, pure Python)

**Job-level preflight.** Nothing is written if any check fails.

1. Take a global write lock: `fcntl.flock` on `data_dir/locks/write.lock`. Only one apply or restore job runs at a time.
2. Recovery must have run (1.7).
3. Per item:
   - `lstat` shows a regular file, not a symlink, with `st_nlink == 1` and owned by the process euid;
   - the realpath is inside its library root;
   - `os.access(file, W_OK)` (needed because `rename()` over a 0444 file succeeds);
   - `os.access(dir, W_OK|X_OK)`;
   - sha256 equals the plan's value.
4. **Space checks** with `statvfs().f_bavail * f_frsize`:
   - the backup filesystem needs the sum of sizes of backups not already in the store, plus a margin (default 64 MiB);
   - each library filesystem needs the largest file × 1.1 + 1 MiB + margin, for the temp copy;
   - the library check is repeated before each file.

**Phase A: back up everything, then verify, before any edit.**

- The backup store is `data_dir/backups/sha256/ab/<sha256>`, content-addressed.
- A backup is written to a temp file opened with `O_CREAT|O_EXCL`, fsynced, re-hashed, then `os.link`'ed to its final name and made read-only (0400). The store directory is fsynced.
- If the final name already exists, it is **re-hashed and reused** only if it matches. Otherwise the job fails. A backup is never overwritten or deleted (S1).
- Every item's backup is re-hashed again before Phase B starts.

**Phase B: per file, sequentially.** Every step is journaled (1.7).

1. Re-check the preflight conditions for this file.
2. Create a temp file in the **same directory**: `.gama-tmp-<job>-<n>-<rand><ext>`, `O_EXCL`, keeping the extension. Its path is journaled *before* creation. It is filled **from the backup** (S7), fsynced, and its hash must equal `sha_before`.
3. **Mutate in a child process** (`python -I -m gama.safewrite.mutate`, 60 s timeout, killed on timeout). The child gets the temp path, format and target `values`/style, applies the change with the policy save options, and exits. The parent then fsyncs the temp file.
4. **Verify the temp file** in a separate read child:
   - (a) the genres read back equal the expected stored `values` exactly;
   - (b) a full snapshot of everything else equals the snapshot taken from the backup:
     - Vorbis: comment list minus genre, in order; vendor; FLAC blocks; pictures incl. type, mime, description and data hash;
     - ID3: every frame except `TCON` by `HashKey` + serialized bytes; version as expected per policy;
     - MP4: every `ilst` item except `©gen`/`gnre`, incl. freeform atoms and `covr` bytes;
     - stream info: length, sample rate, channels, bitrate;
   - (c) the raw ID3v1 trailer and APEv2 bytes are identical;
   - (d) audio packets are unchanged: `ffmpeg -map 0:a -c copy -f md5`, backup vs temp. This runs when ffmpeg is on PATH (default on), and the result records `audio: verified | not_checked`.

   This production check uses mutagen plus raw bytes plus ffmpeg. The fully independent oracle (`tools/verify`) runs in the test suite. A same-library re-read alone cannot see mutagen's own normalisation; the policies in 1.4 address the known cases.
5. Preserve attributes on the temp file:
   - mode (`S_IMODE`);
   - group (`os.chown(tmp, -1, gid)`; failure fails the item);
   - all xattrs, which includes POSIX ACLs (`system.posix_acl_access`). `ENOTSUP` is recorded, any other error fails the item.
   - **mtime is intentionally not preserved**, so Navidrome and other scanners notice the change.
6. Re-hash the original. It must still equal `sha_before`; otherwise the item gets `conflict`, the temp file is removed, and the original is untouched.
7. Journal `renaming` with `sha_after = sha256(temp)`, then fsync.
8. `os.replace(temp, original)`, then fsync the directory.
9. Re-hash the original. It must equal `sha_after`. Then journal `committed`.

Any failure before step 8 removes the temp file and leaves the original byte-identical. A failure after step 7 is resolved **by hash, not by state** (S3).

**Batch policy.**

- Default **stop-and-rollback**: on the first item failure, stop, then restore every item whose current hash is not `sha_before` (committed and `renaming` items) through the restore path. Final status is `rolled_back`, or `failed_partial` if a rollback item hit a conflict.
- Opt-in **continue-on-error** (a checkbox on the confirm page): continue and report. Final status is `completed_with_errors`.
- Items skipped at preview are never written in either mode.

**What is atomic, stated honestly in the UI and docs:**

- Each file is replaced atomically by `rename(2)` on POSIX local filesystems. Readers see either the old or the new file.
- **A multi-file job is not atomic.** Other programs can see a half-applied job while it runs. A crash leaves some files committed, which recovery detects and restore can undo.
- Durability depends on the filesystem honouring fsync. Network and FUSE filesystems may not, or may not rename atomically.
- There is a small window between step 6 and step 8 where another program's write could be lost. Gama cannot lock against other writers.
- Not preserved: inode number, ctime, birth time, owner (files we do not own are refused), and hard links (refused).

### 1.7 Journal, recovery, restore

- **Journal.**
  - One append-only file per job: `data_dir/journal/<job_uuid>.jsonl`, created `O_EXCL` (S4). Every record is written and fsynced.
  - A header record lists the items: root, relpath, `sha_before` and backup sha.
  - Item states: `planned → backed_up → temp_created → temp_written → verified → renaming → committed`, or `failed(stage, error)`, `conflict` or `skipped`.
  - Restore states: `restoring → restored`, `restore_conflict` or `restore_failed`.
  - A torn last line is ignored.
  - The journal is the source of truth for crash recovery. The DB mirrors it for the UI.
- **Recovery** runs at worker start and before every job (`safewrite/recovery.py`). For each journal without a terminal job record:
  1. delete journaled temp files that still exist;
  2. resolve every non-terminal item by current hash: `== sha_after` → `committed`, `== sha_before` → `failed(interrupted)`, anything else → `conflict`;
  3. mark the job `interrupted` (DB updated from the journal).

  Recovery never continues a job automatically. The user reviews the result and can restore.
- **Restore** (a job or selected items) is a **planned, previewed, confirmed job of kind `restore`**, journaled like an apply.
  - For each item, the backup is first re-hashed against `sha_before` (S2). A bad backup means the item is refused and the file is not touched.
  - Then the current file decides:
    - `== sha_after`: restore via temp-from-backup → attribute copy → re-hash → rename → verify `== sha_before`;
    - `== sha_before`: nothing to do;
    - anything else: `restore_conflict`, not restored (S5);
    - missing: reported, never recreated.
  - A per-item "force restore" first backs up the current bytes into the store, so nothing is ever lost.
- **History record.** The `EditPlan` + `EditJob` + `JobItem` rows plus the journal file hold every field the requirements list:
  - timestamp and operation id;
  - affected files;
  - original metadata (genres and the full pre-snapshot);
  - requested modification;
  - per-item success or failure;
  - backup locations (sha → store path);
  - verification results.

  Backups are kept forever by default (Q5). History and the backup store are never pruned automatically.

### 1.8 Running apply, with progress

- `POST /edits/plans/<id>/confirm` (requires the apply permission) creates an `EditJob(status=queued)`.
- **`manage.py gama_worker`** is a separate long-running process (a systemd unit later; a second terminal in development). It:
  - runs recovery at start;
  - polls every second and claims a job with `UPDATE … SET status='running' WHERE id=? AND status='queued'`;
  - runs the job with `heartbeat_at` updates;
  - writes progress (`done/total`, per-item state) to the DB after each journal step.
- **Why a separate process:** it survives web restarts, avoids threads in the ASGI/WSGI server, serializes writes naturally, and recovery has one owner. `--once` runs a single job, and tests call `run_job(job_id)` directly.
- **mutagen runs only in child processes with timeouts**, for both reads (`metadata/readproc.py`: chunks of 50 files, retried one by one on timeout to isolate the hanging file) and writes (`safewrite/mutate.py`). Killing a child can never leave a half-renamed file, because the parent owns every rename. This also avoids the nested-`SIGALRM` bug (S6).
- The job page refreshes every 2 s while the job is `queued` or `running`. It shows a progress bar, the current file and a live table of item states and errors. A stale heartbeat (> 60 s) is shown as "worker not running".
- **Index refresh.** After a job ends (any status), the worker re-reads every touched file from disk and updates `MediaFile`, `Track` and the affected `Album` rows. It also stores `genres_read_back` on each `JobItem`. The result page shows that read-back value next to the requested one.

### 1.9 Auth

- `django.contrib.auth.middleware.LoginRequiredMiddleware` is on, so **every** view requires login. Only login/logout are exempt.
- Writing actions (confirm apply, create or confirm restore) require the custom permission `edits.apply_edits`. It can be granted via the admin or `manage.py gama_grant <user>`. Other logged-in users can browse and preview only.
- CSRF is on all POSTs. Development binds to 127.0.0.1. Production settings require `DEBUG=False`, `SECRET_KEY` from the environment and secure cookies when behind TLS.

### 1.10 Configuration and dev safety

- `gama.toml` + environment variables: `library_roots`, `data_dir` (default `$XDG_DATA_HOME/gama`), `backup_margin_mb`, `verify_audio = auto|off`.
- Startup refuses a `data_dir` inside any library root, and library roots inside `data_dir`.
- **Tests:** `conftest.py` fails the session if any configured root resolves to or under `/storage/music`, and all roots and data dirs come from `tmp_path`.
- `manage.py gama_demo_library <dir>` builds a synthetic library for manual testing.

---

## 2. Layout and models

```
backend/
├── requirements.txt, requirements-dev.txt     exact pins
├── pyproject.toml                             package metadata, pytest config (pythonpath = src, ../tools)
├── manage.py
├── README.md                                  setup (venv), run web + worker, tests
└── src/gama/
    ├── settings.py, urls.py, wsgi.py, asgi.py, config.py
    ├── metadata/            NO Django
    │   ├── model.py         TrackTags(format, fields, genre: GenreField, pictures, snapshot), ReadResult(ok, error, reason)
    │   ├── detect.py        magic-byte sniff → format + mutagen class
    │   ├── genres.py        GenreField, key(), add/remove/replace, validation
    │   ├── policy.py        writability classification + save options per format
    │   ├── formats/         vorbis.py, id3.py, mp4.py: read(), apply_genres(), snapshot()
    │   ├── snapshot.py      canonical non-genre snapshot + raw ID3v1/APEv2 extraction
    │   └── readproc.py      child-process reader with timeouts (JSON over stdout)
    ├── planning/            NO Django
    │   └── planner.py       plan_files(files, op, genres, reader) → [FilePlan], digest()
    ├── safewrite/           NO Django; the only code that opens user files for writing
    │   ├── fsutil.py        sha256, fsync_file/dir, free_space, copy_attrs, failpoints
    │   ├── backups.py       content-addressed store
    │   ├── journal.py       JSONL O_EXCL append + fsync; load (torn-line tolerant)
    │   ├── preflight.py     job + item checks
    │   ├── mutate.py        child entry point: apply genre change to a temp file
    │   ├── verify.py        temp-vs-backup verification (snapshot, raw bytes, ffmpeg)
    │   ├── writer.py        run_apply(items, policy, store, journal, hooks)
    │   ├── restore.py       run_restore(...)
    │   └── recovery.py      recover(journal_dir)
    ├── library/             Django app: models, scanner.py, views (albums, search, basket), templates
    ├── edits/               Django app: models, services.py (glue to planning/safewrite),
    │                        views (operation, preview, confirm, job, history, restore), templates,
    │                        management/commands/ gama_worker, gama_scan, gama_grant, gama_restore, gama_demo_library
    └── templates/base.html, registration/login.html
backend/tests/   unit/ (genres, planner, detect, policy, safewrite), conformance/ (oracle per format), web/ (Django client), e2e/
tools/fixtures/build.py   fully synthetic fixture builder (ported from research harness)
tools/verify/verify.py    neutral oracle (ported research verifier + fixes for V6, V8)
```

**Models** (`library`):

- `LibraryRoot(id, path unique, name)`
- `MediaFile(id, root FK, relpath, size, mtime_ns, inode, format, detected_by, writable_reason null, read_ok, read_error, scanned_at)`, unique `(root, relpath)`
- `Album(id, root FK, identity_key unique, album_dir, album, albumartist, mb_albumid, track_count, disc_count, formats JSON, genres_summary JSON, inconsistent bool)`
- `Track(id, file OneToOne MediaFile, album FK, title, artist, albumartist, album, discnumber, tracknumber, mb_albumid, genres JSON, genre_style, warnings JSON)`

Genre filtering via a Genre M2M is deferred (not needed for this feature).

**Models** (`edits`):

- `EditPlan(id uuid, created_by FK, created_at, operation add|remove|replace|restore, genres JSON, album_ids JSON, album_labels JSON, source_job FK null (restore), status draft|confirmed|superseded, digest, n_change, n_nochange, n_skip)`
- `PlanItem(plan FK, seq, album FK null SET_NULL, album_label, root_path, relpath, format, size, mtime_ns, inode, sha_before, sha_after_expected null (restore), genres_before JSON, genres_after JSON, style, action change|nochange|skip, reason, warnings JSON)`
- `EditJob(id uuid, plan OneToOne, kind apply|restore, created_by FK, policy stop_rollback|continue, status queued|running|succeeded|completed_with_errors|rolled_back|failed_partial|conflict|failed|interrupted, done, total, journal_path, worker_pid, heartbeat_at, started_at, finished_at, error)`
- `JobItem(job FK, plan_item FK, state, sha_before, sha_after, backup_sha, snapshot_before JSON, genres_requested JSON, genres_read_back JSON, verification JSON, error, stage, updated_at)`
- `Backup(sha256 pk, size, store_relpath, created_at, last_verified_at)`

Custom permission: `edits.apply_edits`.

---

## 3. Milestones

Each milestone ends with a green `pytest` and a short note in `backend/README.md`. All tests run on synthetic libraries in `tmp_path`.

### M1: Scaffold, fixtures, oracle
- **Scope:**
  - `backend/` with venv instructions and pinned requirements;
  - the Django project with `config.py`, SQLite (WAL, `synchronous=FULL`), auth middleware, login page, empty home page and admin;
  - pytest + pytest-django and the `/storage/music` guard in conftest.
  - **`tools/fixtures/build.py` rewritten to be fully synthetic.** The research builder copies real files from `/storage/music`, which is not allowed. The new builder uses ffmpeg (sine/noise audio, a generated cover via `lavfi color`), metaflac and raw-byte writers for ID3v2.3/2.4 (multi-value TCON, TXXX, USLT, APIC, UFID, POPM), ID3v1, APEv2 and the ID3-prefixed FLAC. MP4 multi-`data` `©gen` cannot be produced by ffmpeg; mutagen may build *input* fixtures only, and the oracle's raw parser must confirm the result.
  - **The corpus** (session-scoped pytest fixture, copied per test) contains:
    - albums: the three example albums (with fake audio), a multi-disc album both with disc subdirectories and in one directory, two albums with the same title by different artists, two editions with different MB ids, and an album with inconsistent per-track genres and albumartist;
    - formats: FLAC (one and multiple GENRE fields, two pictures, no padding), MP3 v2.4 (multi TCON, cover, lyrics, ID3v1, APEv2), MP3 v2.3 (`Rock/Post-Punk`), true ID3v1-only MP3 (fixes F1), untagged MP3, M4A (AAC moov-after-mdat, faststart, ALAC, `"Rock;Progressive Rock"`, multi-data, `gnre`, freeform atoms), Ogg Vorbis, Opus;
    - broken files: truncated, header-only, zero-byte, garbage, misnamed.
  - **`tools/verify/verify.py`** ported as an importable module (`compare(master, work, mode, expected_genres) → problems`). Closes V6 (decode ID3v1 fields, hash APE items) and V8 (per-frame flags, UTF-16 per value).
- **Acceptance:**
  - fresh venv + `pip install -r requirements-dev.txt` + `pytest` passes on Python 3.14;
  - `runserver` redirects `/` to the login page;
  - the builder creates the corpus in under 30 s;
  - the oracle accepts an unmodified copy and rejects the planted defects listed below.
- **Tests:**
  - builder output inventory (file count; raw checks that each fixture has the intended property, e.g. ID3v1-only has no `ID3` header);
  - **oracle self-tests**, which plant defects: drop a UFID frame, change APIC type, garble the ID3v1 title, edit an APE item, turn a multi-field Ogg GENRE into a single field, change the mode, leave a temp file. Each must FAIL;
  - login required on every URL (iterate the URLconf);
  - config refuses `data_dir` inside a root.

### M2: Metadata read path
- **Scope:** `metadata/` (detect, formats read, GenreField, policy classification, snapshot, readproc).
- **Acceptance:**
  - every corpus file returns either a `ReadResult` with correct genres or `ok=false`/`skip` with a reason;
  - no exception escapes `read()`;
  - a hanging reader is killed and reported as a timeout.
- **Tests:**
  - per format, `components`/`values`/`style` match the oracle's raw genres;
  - multi-genre on one track, for every format;
  - delimited MP4 and delimited FLAC; mixed → skip;
  - v2.3 `Rock/Post-Punk` is one value; numeric TCON is resolved;
  - `gnre` read by name;
  - `load_v1=False` (the v1-only file shows no genre and is classified `skip: id3v1-only`);
  - APEv2 Genre warning;
  - classification of every unsupported or corrupt fixture (truncated FLAC/MP3 may be readable; truncated M4A is refused);
  - misnamed file: detected by content, then skipped for writing;
  - readproc timeout via an injected sleeping reader (test-only hook);
  - snapshot stability: two reads produce equal snapshots.

### M3: Genre operations and planner (pure)
- **Scope:** `metadata/genres.py` ops + validation; `planning/planner.py` (file list + op → FilePlans with action/reason/warnings, digest).
- **Acceptance:** 100 % branch coverage of `genres.py`; the planner needs no Django.
- **Tests:**
  - **adding an existing genre** (exact and different case → nochange plus a case note);
  - **removing a nonexistent genre** → nochange;
  - remove all case variants and duplicates;
  - remove the last genre → field deleted;
  - add to empty;
  - add to delimited keeps the delimiter and spacing;
  - replace dedupes the input; replace that equals the current values → nochange;
  - input validation (`;`, empty, control characters);
  - **multiple genres on one track**;
  - **inconsistent track metadata** (tracks of one album with different genre sets → per-file results; the preview groups them correctly);
  - **unsupported/corrupt files** become `skip` items with reasons and are counted;
  - **idempotence property test** (hypothesis is optional; a table-driven test is enough): `op(op(x)) == op(x)`, and planning the same op on the result gives 0 changes;
  - the digest changes when any item changes.

### M4: Format writers (mutate a temp copy)
- **Scope:**
  - `formats/*.apply_genres` plus the save options from `policy.py` (ID3v2.4 save, v2.3→v2.4, ID3v1 trailer restore, APE untouched, `gnre` removal);
  - `safewrite/mutate.py` (child entry point);
  - `safewrite/verify.py` (snapshot, raw, ffmpeg).
- **Acceptance:** for every writable corpus file and each of add/remove/replace, mutating a copy and then checking it with the **oracle** reports zero problems. `safewrite/verify.py` agrees with the oracle on all planted-defect cases it covers.
- **Tests:**
  - conformance matrix: format × op, checked by the oracle;
  - **preservation of unrelated tags and artwork**: two-picture FLAC, APIC, `covr`, `METADATA_BLOCK_PICTURE`, lyrics, TXXX, UFID, POPM, freeform atoms, ReplayGain, Unicode custom tags, Vorbis key case and order;
  - audio packet md5 unchanged (faststart M4A included);
  - ID3v1 trailer bytes identical;
  - APE bytes identical;
  - v2.3 file becomes v2.4 with only the expected date conversion;
  - delimited M4A stays a single delimited `©gen`;
  - multi-data M4A stays multi-data;
  - Ogg genres end up as separate fields (V1);
  - `verify.py` rejects deliberately wrong genres and a deliberately dropped frame.

### M5: Safe write engine (backups, journal, preflight, apply, restore, recovery)
- **Scope:** all of `safewrite/` with failpoint hooks (`fsutil.failpoint(name)`, which can raise or `os._exit` at each step) and an injectable `free_space`.
- **Acceptance:** S1–S7 and S9 each have a regression test. No test leaves temp files. Originals are byte-identical after every failure before rename.
- **Tests:**
  - happy path, with the oracle on the result;
  - **missing permissions**: 0444 file, read-only directory, file owned by another uid (simulated by stubbing the euid check) → preflight aborts with zero writes;
  - **insufficient storage for backups**: fake `free_space` below the requirement → abort, zero writes, no partial backup left; plus a library-fs shortage before file N → that item fails cleanly;
  - **file changing between preview and application**: modify after planning → job `conflict`, zero writes; modify between temp write and rename (failpoint callback) → item `conflict`, original keeps the new bytes;
  - **interrupted operations**: run apply in a subprocess with `os._exit` at each failpoint (`after_backup`, `after_temp_written`, `after_renaming_journaled`, `after_rename`, `after_commit`), then `recover()` → temps removed, states resolved by hash, job `interrupted`;
  - **partial failure**: an unwritable file mid-batch (made 0444 after preflight via failpoint) under `stop_rollback` → all earlier files restored to `sha_before`; under `continue` → the others committed and the failure reported;
  - **restoration from backups**: restore after success → byte-identical originals and verified hashes; restore when a file was edited later → `restore_conflict`, file untouched; corrupt backup → refused before touching the file (S2); force restore backs up the current bytes first;
  - backup reuse across two jobs on the same file: no overwrite, and restoring job 1 still works (S1); journal name collision refused (S4);
  - symlink and hardlink refused;
  - mode, group and `user.*` xattr preserved (skip the xattr test if the tmp fs returns ENOTSUP);
  - **repeated operation**: applying the same plan op twice → the second plan has 0 changes, and no new backups are created for unchanged files.

### M6: Library index and album selection
- **Scope:**
  - `library` models and migrations;
  - `scanner.py` (scandir walk, ignores `.gama-tmp-*`, size+mtime diff, readproc parse, album identity rebuild);
  - `gama_scan`, `gama_demo_library`;
  - album list with search (album, artist, directory), checkbox selection into a session basket, and a basket page that stays visible in later steps.
- **Acceptance:** scanning the corpus gives the expected album groupings, and a rescan after an external change updates only that file.
- **Tests:**
  - **single and multi-disc albums** (disc subdirectories and a flat directory with `discnumber` → one album each, correct disc count);
  - duplicate titles → separate albums;
  - editions (MB id / directory) → separate;
  - **inconsistent track metadata** → the directory splits as specified and is flagged `inconsistent`;
  - unreadable files indexed with `read_ok=false`;
  - temp files ignored;
  - basket persists across requests;
  - login required.

### M7: Edit workflow: plan, preview, confirm, worker, progress, re-index
- **Scope:**
  - `edits` models;
  - services wrapping planner/safewrite;
  - views and templates for **choose operation** (three clearly separate actions: Add and Remove first, Replace on its own page with a warning and an extra confirmation checkbox), **preview**, **confirm** (permission + digest), the **job/progress page** and the **results page** (requested vs read-back genres, per-item errors, verification summary);
  - `gama_worker` (claim, heartbeat, recovery at start, post-job re-index);
  - `gama_grant`.
- **Acceptance:** with the Django test client and `run_job()` called synchronously, a user can go select → preview → confirm → apply → results on the synthetic library. The index reflects the genres read back from disk.
- **Tests:**
  - preview shows every album and track, before and after, change/nochange/skip counts and warnings;
  - **adding an existing / removing a nonexistent genre** appear as "no change";
  - no file is written by any GET or by preview (hash every corpus file before and after);
  - confirm without the permission → 403; without login → redirect;
  - a wrong digest or double confirm → rejected;
  - **conflict between preview and apply** → job `conflict`, the page offers refresh, and the refreshed plan shows the new state;
  - **missing permissions / partial failure** shown per item on the job page;
  - **interrupted**: a job left `running` with a dead worker (journal mid-way) → the worker's startup recovery marks it `interrupted`;
  - re-index: Track genres equal a fresh disk read after apply;
  - the worker processes queued jobs in order and ignores jobs that are not queued.

### M8: History and restore UI, end-to-end, docs
- **Scope:**
  - history list and job detail (all record fields, backup locations);
  - restore flow (select job or items → restore preview with restore/nothing-to-do/conflict per file → confirm → job);
  - `gama_restore` CLI as a fallback when the web UI is down;
  - an e2e test that runs the real `gama_worker --once` as a subprocess;
  - docs: `docs/user-guide/genre-editing.md` (workflow, what is and is not atomic, policies, recovery), ADR `docs/adr/0002-genre-editing-policies.md`, updates to architecture.md for the deviations in 1.1.
- **Acceptance:** the definition of done is shown end to end on the three example albums. A reviewer can follow the docs to recover from a bad edit.
- **Tests:**
  - **successful restoration** via the UI → originals byte-identical, index updated;
  - restore preview shows conflicts for later-edited files and does not touch them;
  - **repeated operations / idempotence** e2e: apply "Add Progressive Rock" twice → the second preview shows 0 changes;
  - **single + multi-disc** e2e: one selection edits every track on both discs;
  - the oracle runs on the whole library after the e2e apply (zero problems, only the intended files changed);
  - the history page contains every field required by requirements §5.

### Requirements §7 → milestones

| Test item | Milestones |
|---|---|
| Single and multi-disc albums | M6, M8 |
| Multiple genres on one track | M2, M3, M4 |
| Adding an existing genre | M3, M7 |
| Removing a nonexistent genre | M3, M7 |
| Preserving unrelated tags and artwork | M4, M5, M8 (oracle) |
| Albums with inconsistent track metadata | M3, M6 |
| Unsupported and corrupted files | M2, M3, M7 (preview warnings) |
| Missing permissions | M5, M7 |
| Insufficient storage for backups | M5 |
| File changing between preview and application | M5, M7 |
| Interrupted or partially failed operations | M5, M7 |
| Successful restoration from backups | M5, M8 |
| Repeated operations and idempotence | M3, M5, M8 |

---

## 4. Open questions (defaults let work continue)

| # | Question | Default |
|---|---|---|
| Q1 | ID3v2.3 files: upgrade to v2.4 on write, keep v2.3 with a `/`-joined genre string (does not round-trip), or skip them? | Upgrade to v2.4, warned per file in the preview |
| Q2 | ID3v1 trailer: keep byte-identical (may show a stale genre in v1-only readers) or mirror the first genre into the v1 genre byte? | Byte-identical |
| Q3 | Adding a second genre to an MP4 that has one plain value: multiple `data` atoms (native; some players show only the first) or a `;`-joined string (as some existing files use)? | Native multi-`data`. Files already using `;` keep `;` |
| Q4 | Separators recognised inside a single value: only `;`? | Only `;`. `/` and `,` are never split |
| Q5 | Backup retention | Keep forever. Show store size on the history page. A prune command is a later feature |
| Q6 | Batch failure default: stop and roll back, or continue? | Stop and roll back; continue is opt-in per job |
| Q7 | Audio-packet check with ffmpeg in production (slower: one decode-free remux pass per file) | On when ffmpeg is present |
| Q8 | Files Gama cannot preserve exactly (not owned by the Gama user, hard links, symlinks, ID3v1-only, FLAC with ID3 prefix): refuse or allow? | Refuse with a warning. Deployment must run Gama as the user that owns the library |
| Q9 | Max files per plan, since preview hashes every file inside the web request | 2,000 files. Above that, the user is asked to narrow the selection |

**Risks:**

- mutagen open issues: #754 (duplicate GEOB/RVA2 dropped on load; the snapshot check will refuse such files at verify time), #748 (fragmented MP4, refused at preflight), and slow releases. mutagen stays pinned, and the conformance suite reruns on every upgrade.
- A full-file sha256 on large libraries makes the preview slow. This is accepted, because safety comes before speed.
- External writers (sync services) can race the rename window. This is documented and cannot be fully prevented.
- An Arch Python upgrade breaks the venv. The README documents rebuilding it.
- Before the first write to `/storage/music`, the user should do a read-only scan of it and a write test on a copied sample (per architecture.md). That needs explicit user approval and is outside this plan.
