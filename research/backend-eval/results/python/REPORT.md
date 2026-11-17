# Python / mutagen: backend evaluation report

Candidate: **python**. Prototype: `python/proto` (a shell wrapper around `python/proto.py`, which uses the venv at `python/.venv`).
Run date: 2026-11-17. Host: Arch Linux, kernel 7.2.15, ffmpeg n9.0.2 (the verifier's tool).

| | |
|---|---|
| Language / toolchain | CPython **3.14.10** (system `/usr/bin/python3.14`), venv + pip 26.2.2 |
| Metadata library | **mutagen 1.48.1** (PyPI, released 2026-06-25), pure Python, licence **GPL-2.0-or-later** |
| Prototype size | `proto.py` has 680 lines (599 non-blank). The safety protocol is about 210 lines: journal + restore 55, preflight, batch write and rollback 157. Snapshot and verify helpers add about 60 more |
| Helper scripts (not part of proto) | `compare_ffprobe.py` (E1), `inspect_edge.py` (E4), `side_experiments.py`, `make_fuzz.py`, `measure.py` (stands in for `/usr/bin/time`, which is not installed) |

## Result table

| Exp | Result | Evidence |
|---|---|---|
| E1 read | 34 files: **29 ok, 5 ok:false** (the 4 genuinely broken files and `zero_bytes.mp3`). No crash, no hang. With mutagen's default detection, `edge/actually_flac.mp3` **fails** (`HeaderNotFoundError: can't sync to MPEG frame`); proto adds a content-sniff fallback, after which it reads as FLAC and matches ffprobe. Value differences against ffprobe are listed below the table | `read.json`, `e1_vs_ffprobe.txt` |
| E2 set-genres albums | 15/15 committed, in-process verification passed. Strengthened verifier, run on each album separately with `--require-changed`: **15/15 ok, 0 problems**. This includes raw ID3v2 TCON and the frame inventory for MP3. Note on `real_mp3` ×3: "existing ID3v1 tag re-synced from new values". Audio PCM and packets identical everywhere, pictures identical, no leftover files | `e2_verify.txt`, `e2_verify_<album>.json`, `e2_set_genres_*.json`, `e2_read_after.json` |
| E3 set-fields | 15 committed (5 album formats + 10 writable edge files), 4 refused at preflight (garbage, header-only, truncated M4A, zero-byte). Verifier: **14 ok, 1 FAIL**: `mp3_id3v1_only.mp3` "ID3v2 frame TCON changed: None -> Rock" (a *real* mutagen behaviour, see below). Notes: `mp3_id3v23` reports "ID3v2 version 2.3 -> 2.4"; MP3s with an ID3v1 tag report "existing ID3v1 tag re-synced" | `e3_verify.txt`, `e3_set_fields.json` |
| E4 set-genres edge | 10 committed, 4 refused at preflight. Verifier: **13 ok, 1 FAIL**: `mp3_id3v1_only` (real ID3v1→v2 promotion: TALB, TDRC, TIT2, TPE1 and TRCK were added). `mp3_id3v23` is ok, with notes for the 2.3→2.4 conversion (TYER+TDAT → TDRC `1979-06-15`). `mp3_id3v24_apev2_id3v1` is ok, with an ID3v1 re-sync note | `e4_verify.txt`, `e4_edge_details.txt`, `e4_set_genres.jsonl` |
| E5a no `--continue-on-error` | Aborted at preflight (`03 truncated.m4a` unparsable, `05 readonly.m4a` not writable). **sha256 of all 5 files unchanged**, no temp files | `e5_log.txt`, `e5a.json` |
| E5b with flag | 3 committed, 2 skipped and reported. The read-only file stays mode 0444 and unchanged. Verifier: **5/5 ok, 0 problems** (mp3 note: ID3v1 re-synced) | `e5b.json`, `e5b_verify.txt` |
| E5c `--fail-after 2` | 6 healthy files (flac+mp3). 2 committed, then the simulated failure; both rolled back and verified by sha256. **All 6 files identical to before**. Journal states `rolled_back` ×2, no temp files | `e5c.json`, `e5_log.txt` |
| E5d `rollback --journal` | Successful 3-file m4a batch, then rollback: 3/3 restored, sha256 matches, mode 644 kept | `e5d_write.json`, `e5d_rollback.json` |
| E6 resources | No build step. Fresh venv + `pip install mutagen` takes 1.6 s (warm pip cache). Artifact: proto.py 28 KB + mutagen 1.7 MB (venv 16 MB, mostly pip). Runtime deps: a Python ≥3.10 interpreter and mutagen; **no C extensions, no native libs**. `read` over the working copy (34 files, 178 MB): **wall 0.075 s, peak RSS ≈ 29.8 MB** (3 runs; interpreter + import alone is 0.022 s / 16 MB). set-genres on 9 real files (85 MB) with full protocol: 0.28 s, 32 MB | `e6_resources.txt` |
| Extra: fuzz | Read: 420 corrupted/truncated copies → 256 ok, 164 errors, **0 hangs, 0 process crashes**, max 15 ms per file. **3 errors were not `MutagenError`** (2× `IndexError` in Ogg, 1× `ValueError: bad sequence number` in Opus). Write: 288 small fuzzed files → 111 committed, 164 preflight-refused, 13 Ogg files passed preflight but **failed during `save()`** on corrupt later pages. Originals untouched, 0 temp files left | `fuzz_summary.txt`, `fuzz_write_summary.txt` |

### Verifier excerpts (strengthened `harness/verify.py`, re-run on the original E2–E5b work dirs)

```
E2 (per album, --require-changed): real_flac 3/3, real_mp3 3/3, real_m4a 3/3,
    synth_vorbis 3/3, synth_opus 3/3 ok; 0 with problems
    real_mp3/*: . note: existing ID3v1 tag re-synced from new values
E3: FAIL changed   edge/mp3_id3v1_only.mp3
       - ID3v2 frame TCON changed: None -> [('text', ('Rock',))]
       - genre changed during field write: None -> 'Rock'
    ok   changed   edge/mp3_id3v23.mp3      . note: ID3v2 version 2.3 -> 2.4
    34 files, 15 changed, 1 with problems
E4: FAIL changed   mp3_id3v1_only.mp3
       - ID3v2 frame TALB changed: None -> [('text', ('V1 Album',))]   (+TDRC, TIT2, TPE1, TRCK)
       - unrelated tag 'album' changed: '<absent>' -> 'V1 Album'       (+artist, date, title, track)
    ok   changed   mp3_id3v23.mp3
       . note: ID3v2.3->2.4 converted TYER/TDAT -> TDRC '1979-06-15'
    14 files, 10 changed, 1 with problems
E5b: 5 files, 3 changed, 0 with problems
```

Earlier runs of the verifier, which used ffprobe alone and so saw only the first ID3v2.4 TCON value, reported false MP3 genre failures. The strengthened verifier reads raw TCON and resolves them. My own mutagen-free byte check agrees: `python/id3_tcon_bytes.py` → `mp3_tcon_bytes.txt` shows `\x03Ambient\x00Electronic\x00Soundtrack\x00`. mutagen writes a trailing NUL after the last value, and the master fixtures have one too. The only remaining verifier failure, `mp3_id3v1_only`, is a real side effect.

## Format behaviour

### Multi-value genres

| Format | What mutagen does | Verified by |
|---|---|---|
| FLAC | `tags["genre"] = [...]` produces repeated `GENRE=` Vorbis comments. Existing GENRE fields are removed, and new ones are appended **at the end** of the comment list. Non-genre comment order is preserved (the metaflac order check passes) | verifier + metaflac |
| Ogg Vorbis / Opus | Same as FLAC (repeated fields). `METADATA_BLOCK_PICTURE`, MusicBrainz, ReplayGain and the unicode custom tag are preserved | verifier |
| MP3 ID3v2.4 | `TCON(encoding=3, text=[...])` produces a single TCON frame with NUL-separated values (correct v2.4) | verifier (raw TCON) + own byte walker |
| MP4 | `tags["©gen"] = [...]` writes **one `©gen` atom with multiple `data` atoms**. ffprobe joins these as `Ambient;Electronic;Soundtrack`. Any `gnre` (numeric) atom must be deleted by hand, and proto does this | verifier |
| ID3v2.3 input | mutagen **always translates v2.3 to v2.4 frames in memory on load**, and `save()` defaults to `v2_version=4`. The file is upgraded to **v2.4**: `TYER 1979` + `TDAT 1506` are merged into `TDRC 1979-06-15`, and `TYER`/`TDAT` disappear. All other frames are kept, including `TXXX:CUSTOM_UNRELATED`, `TXXX:lyrics-eng`, APIC, TENC and TSSE. The original v2.3 `TCON "Rock/Post-Punk"` is **read as one genre** `Rock/Post-Punk` (mutagen does not split `/`). Keeping v2.3 is possible with `save(v2_version=3, v23_sep=...)`, but multiple values then become **one joined string** (`Ambient/Electronic/Soundtrack`). There is also a footgun: unless you call `tags.update_to_v23()` first, mutagen writes a **TDRC frame into a v2.3 tag**, which is invalid in v2.3 (`side_experiments.txt`) | `e4_edge_details.txt`, `side_experiments.txt` |

### Pictures

FLAC `PICTURE` blocks (including the **two-picture** file, types 3 and 4), ID3 APIC, MP4 `covr` and Ogg `METADATA_BLOCK_PICTURE` were preserved byte-identically in every write. The verifier compares picture stream md5s, and proto compares raw picture bytes before and after. MP4 `covr` has no picture-type field, so `type` is `null`.

### Unknown and unrelated frames

- **Preserved:** USLT lyrics, TXXX, TENC, TSSE, Vorbis custom tags, MP4 freeform `----` atoms.
- **APEv2 trailer** (`mp3_id3v24_apev2_id3v1.mp3`): left untouched, and `REPLAYGAIN_TRACK_GAIN`/`MP3GAIN_MINMAX` survive. mutagen's `MP3` class **does not expose APEv2 at all**, so proto reads it separately with `mutagen.apev2.APEv2`. A Gama scanner would have to do the same.
- **ID3v1 trailer:** with the default `save(v1=1)`, mutagen **rewrites an existing ID3v1 tag from the new v2 data**. The genre byte changed from 17 (Rock) to 26 (Ambient). This is consistent, but it is a change outside the "genre in v2" scope.
- **"ID3v1-only" file** (`mp3_id3v1_only.mp3`): this fixture already has a small 34-byte ID3v2.4 tag containing only `TSSE`, plus the ID3v1 trailer. On load, mutagen **merges the ID3v1 fields into the in-memory ID3v2 tag** (`load_v1=True`, which fills frames missing from v2). On any save, the V1 title, artist, album, year, track and genre are therefore **added to the existing ID3v2 tag**; no new tag is created. That is a silent metadata change, and the verifier correctly flags it. My in-process verify could not catch it because the pre-read already contained the merged v1 frames. Fix for Gama: load with `ID3(load_v1=False)`, or detect v1-only files and decide on a policy.
- **ID3v2 prefix on FLAC** (`flac_with_id3v2_prefix.flac`): mutagen skips it on read and **keeps it on save** (`deleteid3=False`). It is **not updated**, so it now holds stale tags. Other readers that prefer ID3 could show old values.
- **Padding:** FLAC files without padding get a full rewrite plus a **3100-byte PADDING block** added (+3.1 KB). Ogg grows by about 2.7 KB (pages re-laid out). The faststart M4A grew by 14 KB: moov before mdat, so mutagen moved mdat and updated `stco`; packets are identical. For moov-after-mdat M4A and MP3 the size did not change, because existing padding was reused.

### Audio-data changes

**None detected.** Decoded PCM md5 and compressed-packet md5 matched master for every changed file in E2, E3, E4 and E5 (verifier). This includes faststart M4A, ALAC, truncated FLAC/MP3 and FLAC data in a `.mp3` file.

## Malformed files

- **No panics or hangs.** mutagen is pure Python. Max read time was 15 ms per file, including fuzz. `read` runs detection and tag extraction under separate sequential `SIGALRM` deadlines (30 s each); none fired.
- **Bug in proto (reviewer S6): writes effectively run without a timeout.** `write_one` wraps the edit in `deadline(120 s)`, but the `detect()` call inside it opens its own nested `deadline()`. On exit, that inner deadline calls `signal.alarm(0)` and restores the handler, which cancels the outer alarm. So `save()`, the re-read and the verification run with **no deadline**. No write hung in any experiment or the write fuzz, so the results are unaffected, but the hang guard claimed for writes does not exist. Fix: make the deadline re-entrant (save `signal.alarm(0)`'s remaining time and re-arm it on exit), or use a watchdog subprocess or thread for writes.
- **Errors are mostly `MutagenError` subclasses, but not all.** Fuzzing produced `IndexError` (Ogg) and `ValueError` (Ogg page sequence). Callers must catch `Exception`, not just `MutagenError`. An upstream fix for the Ogg `IndexError` was committed on 2026-08-10 but is **not yet in a release**.
- **Misdetection:** `mutagen.File` scores the extension and header bytes together. For FLAC data in a `.mp3` file, FLAC and MP3 both score 1 and MP3 wins on list order, so the read fails. proto falls back to a magic-byte sniff and reports `detection.method = "content sniff fallback"`. Gama would need this too.
- `garbage.flac` and `zero_bytes.mp3` give clean errors (`FLACNoHeaderError`, `HeaderNotFoundError`). ffprobe "opens" `garbage.flac` as FLAC by extension and finds no tags.
- **Truncated FLAC and MP3 parse and write fine**, because mutagen only reads the metadata area. Preflight therefore cannot detect audio truncation. A truncated M4A with a missing moov is refused (`MP4StreamInfoError: not a MP4 file`).
- Corrupt Ogg files can **pass preflight and then fail inside `save()`**, because mutagen renumbers or rewrites all later pages. The temp-copy design contained this: 13/13 such failures left the originals untouched.

## Safety protocol: implementation notes

- **Writing to an arbitrary path works.** mutagen accepts a path or a file object (`filething`), so step 3 is direct: copy to `mkstemp(dir=same dir, suffix=same ext)`, open with the class detected for the original, `save()`, then `fsync`. I kept the extension on the temp name because `mutagen.File` detection uses it. Open issue #729 (2026-09-18) reports problems saving FLAC through file-like objects, so I used paths.
- **Read-only files:** `mutagen.save()` on a 0444 file raises `MutagenError: [Errno 13] Permission denied`, so the OS refuses. But **`os.replace(tmp, readonly_file)` succeeds**, because only directory write permission is needed, and that silently turns 0444 into 0644. The rename-over protocol therefore *needs* the explicit `os.access(W_OK)` preflight check, and proto has one.
- Step 4 in-process verification uses a snapshot of all tags except the target keys, plus pictures and stream info. For ID3 it compares re-serialized frame bytes. The **weakness**: mutagen normalizes on load (v2.3→v2.4 translation, v1 merge), so the before and after views are both post-normalization. That hides the v1-promotion and v2.3-upgrade changes. An independent check (ffprobe or raw bytes) is needed for those.
- **Mode** is preserved (chmod before rename). **Owner, xattrs, ACLs and hard links are not**: rename creates a new inode. I did not test this. Gama should note it.
- Journal: JSON written via tmp + fsync + rename + dir fsync. Backups are verified by sha256 before any edit. Rollback restores via temp + rename, then checks sha256.
- Not handled: SIGKILL in the middle of a batch. Journal state remains, and `rollback --journal` can then restore the files, but this is untested for a real kill. Concurrent writers are not locked.
- Difficulty: **low**. Python's stdlib (`tempfile`, `os.replace`, `os.fsync`, `hashlib`, `shutil`) covers everything. The main friction was mutagen's per-format APIs: ID3 frames, MP4 atom names and Vorbis dicts are three different models, and `MP3` hides APEv2. mutagen's `easy=True` layer exists but cannot express everything needed, for example `gnre` cleanup and v1 policy.

## Ecosystem (checked 2026-11-17)

- **mutagen** ([PyPI](https://pypi.org/project/mutagen/), [GitHub quodlibet/mutagen](https://github.com/quodlibet/mutagen)): licence GPL-2.0-or-later. This matters if Gama is distributed under a non-GPL licence. The Python process importing it would make Gama a combined GPL work. ~2.0k stars, 219 forks, 149 open issues, last push 2026-11-03.
- **Release cadence is slow and bursty:** 1.45.1 (2020-07), 1.46.0 (2022-10), 1.47.0 (2023-09), then **nearly 3 years** to 1.48.0 (2026-06-22) and 1.48.1 (2026-06-25). Commits continue: Ogg robustness fixes in 2026-08, WAV scoring in 2026-11.
- **Open issues relevant to tag writing:**
  - #748 (2026-10-31): MP4 save breaks fragmented files, because only the first `moof`'s `tfhd` is updated
  - #754 (2026-11-13): duplicate GEOB and incomplete RVA2 frames are silently dropped on load, so they would be lost on save
  - #729: FLAC save through file-like objects
  - #666 (2024): MP3s corrupted after writing ID3
  - #584 (2022): `MP4.save()` breaks phone videos
  - #551 (2022): multi-genre MP4 via freeform atoms
- **Documentation:** good API docs at mutagen.readthedocs.io, with per-format specifics. Caveats such as `update_to_v23()` before a v2.3 save are documented but easy to miss.
- **Adoption:** the de facto Python tagging library. Used by Quod Libet (its origin), MusicBrainz Picard, beets and yt-dlp (optional dependency). Packaged in Arch `extra` (`python-mutagen 1.48.1-1`, confirmed via `pacman -Si`) and in Debian as `python3-mutagen` (version not checked here, since apt is unavailable).

## Application stack for criterion 2 (research only, not built)

Versions are from PyPI on 2026-11-17.

| Concern | Choice | Why |
|---|---|---|
| HTTP | **FastAPI 0.163** on Starlette 1.10 + **uvicorn 0.54** | Largest ecosystem; typed request/response models through Pydantic; OpenAPI for free. Arch packages `python-fastapi` and `uvicorn`. Alternative: Litestar 2.25 (MIT), which is leaner but has a smaller community |
| SQLite + migrations | stdlib `sqlite3` (WAL mode) with **SQLAlchemy 2.x Core** + **Alembic 1.20** | Alembic is the standard migration tool. Use Core rather than the ORM to keep queries explicit. Arch ships SQLAlchemy 2.0.x and Alembic 1.20.1. Alternative for a minimal-dependency build: `apsw` plus hand-rolled `PRAGMA user_version` migrations |
| FS watching / scanning | **watchfiles 1.3** (Rust `notify` underneath, MIT) + `os.scandir` for full scans | Async-friendly, handles inotify and coalescing. Full scans with `os.scandir` + mtime/size are fast enough. Tag parsing is about 0.5 ms per file in E6, so a 100k-file library takes roughly 1 minute single-process; use `ProcessPoolExecutor` to parallelise |
| Background jobs | In-process `asyncio` worker + a SQLite-backed job table (own code); or **huey 3.5** with its SQLite storage | Single-user, self-hosted: avoid Redis/Celery. Tag writes must be serialized per file anyway |
| Auth | Session cookie (Starlette `SessionMiddleware` / itsdangerous) + **argon2-cffi 25.1** for password hashes | Simple, no external IdP needed. Add OIDC later via Authlib if required |
| Testing | **pytest 9.2**, **hypothesis 6.170** (property tests for tag round trips), httpx `TestClient`; plus the ffprobe-based verifier as an integration oracle | Standard |
| Packaging | Arch: PKGBUILD using `python-build`/`python-installer`, with dependencies on repo packages (all key dependencies are already in `extra`). Debian: `dh-python`/pybuild. For distro-independent builds: a `uv`-built venv tarball or zipapp (`shiv`) | Pure-Python dependencies plus watchfiles/pydantic-core wheels. No compile step for mutagen. A runtime interpreter is required, and Python minor upgrades on Arch break venvs (rebuild needed) |

## Bottom line

- mutagen read and wrote every healthy file correctly.
- Audio data was never changed, pictures and unrelated tags were preserved, and all three native multi-value genre forms were written.
- The safety protocol was easy to write and worked in every E5 scenario.

The risks are behaviour hidden by normalization:
- silent ID3v1→v2 promotion
- forced v2.3→v2.4 upgrade (and a footgun if you keep v2.3)
- APEv2 invisible to the MP3 class
- a stale ID3 prefix on FLAC
- extension-first detection
- non-`MutagenError` exceptions on fuzzed Ogg
- GPL licence
- slow release cadence

Performance and footprint are fine for this workload: 75 ms and 30 MB for the fixture scan.

Proto defect: the write-path timeout is cancelled by the nested deadline in `detect()` (reviewer S6), so writes are unguarded against hangs.
