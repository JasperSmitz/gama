# Second-opinion review: backend evaluation (python / go / rust)

Read-only review. Every experiment ran in fresh `mktemp -d` dirs under
`$EVAL/..` (`reuse-*`, `vgap*`), which are left in place for the lead to clean up.
`EVAL=/tmp/claude-1000/-storage-development-gama/88084e20-e91b-48dc-a0ff-99d209baba8d/scratchpad/eval`.

Severity: **H** = can lose a user's original or make a safety claim untrue;
**M** = gap that hides a real problem; **L** = minor, nit, or robustness only.

---

## 1. Safety protocol bugs

### S1 (H, python + rust; go is safe): reusing a backup dir overwrites the only copy of the original
- **Where:** `python/proto.py:519-520` (backup name is `f"{len(entries):04d}-{sha(path)[:12]}-{name}"`, then `shutil.copy2`). `rust/src/safe.rs:300-301` (backup name is `{i:04}-{name}`, then `fs::copy`, which truncates). Go uses `O_EXCL` at `go/write.go:354` and refuses.
- **Trigger:** run two batches on the same file with the same `--backup-dir` (journal J1, then J2), then `rollback --journal J1`. Each new journal starts at index 0, so batch 2 writes to the same backup name and overwrites batch 1's backup with the already-modified file.
- **Evidence:** ran it on a copy of `synth_opus/01`:
  ```
  python: b1 rc=0, b2 rc=0, rollback j1 rc=1 "sha256 mismatch after restore"  -> ORIGINAL NOT RESTORED, no copy of it exists anywhere
  rust:   same; "restored sha256 2425c9… != bd2e99…"                         -> ORIGINAL NOT RESTORED
  go:     b2 rc=1 "backup: open …/bk/0000-a.opus: file exists"; rollback j1 -> original restored
  ```
  (dirs: `$EVAL/../reuse-python-EVeA`, `reuse-rust-SLlF`, `reuse-go-OQ0c`)
- **Fix:** create backups with `O_EXCL` (`open(..., 'xb')` / `OpenOptions::create_new`), or add a batch id or random suffix to the name, or use a fresh per-batch subdirectory.

### S2 (H, python + rust; go is safe): rollback replaces the user's file before checking the backup
- **Where:** `python/proto.py:455-476` `restore_entry` and `rust/src/safe.rs:341-361` `restore`. Both copy the backup over the file and only then compare sha256. Go checks first (`go/write.go:446`).
- **Trigger:** the backup is corrupt, truncated or overwritten (S1, disk error, user edit). Rollback replaces the current file with the bad bytes, then reports an error. That is how the S1 run ended up with a wrong file in place.
- **Fix:** hash the backup and require `== sha256_before` before the temp copy and rename. If it doesn't match, leave the current file untouched.

### S3 (M, all three): a failure after the rename leaves a modified file that the batch rollback skips
- **Where:**
  - python `write_one` (`proto.py:560-566`): if `fsync_dir`, `sha256_file` or `j.mark` raises after `os.replace`, the entry is marked `failed`. `cmd_write` then rolls back only `committed` entries (`:619`).
  - go `writeOne` (`write.go:418-427`): after `committed=true`, a `syncDir` or `j.save` error returns err. `runBatch` sets the state to `failed` (`:330`), and `rollbackCommitted` skips it (`:305`).
  - rust `commit_one` (`safe.rs:328-330`): if `fsync_dir` fails after the rename, the state is still `backed_up`. `cmd_write` turns it into `failed` (`:457-463`), and `rollback_entries(…, true)` skips it.
- **Trigger:** EIO or ENOSPC on the directory fsync or journal write right after the rename, or (python) a signal at that point. The output says "aborted, rolled back", yet one file stays modified and its journal state says `failed`.
- **Fix:** journal a `renaming` state before the rename. In the batch rollback, restore every entry whose current sha differs from `sha256_before` (or every entry that is not `backed_up` and not `failed-before-rename`), not just `committed` ones.

### S4 (M, all three): the journal path is overwritten without warning on reuse
- **Where:** `proto.py:597-600` (`Journal(journal)` + `save()`), `write.go:256,294`, `safe.rs:429-440`.
- **Trigger:** a second batch with the same `--journal` replaces the first batch's journal, so the earlier batch can no longer be rolled back from it. Combined with S1, the original is lost.
- **Fix:** refuse an existing journal (`O_EXCL`), or append to it.

### S5 (M, all three): `rollback --journal` overwrites edits made after the batch
- **Where:** python `cmd_rollback` (`proto.py:633-646`), go `cmdRollback` (`write.go:488-510`), rust `cmd_rollback` (`safe.rs:384-400`). Each records `sha256_after` but never compares it with the current file.
- **Trigger:** a batch commits, the user or another tool edits the file, then rollback runs. The newer edit is silently replaced by the pre-batch backup.
- **Fix:** restore only when the current sha is `sha256_after` (or `sha256_before`, which is a no-op). Otherwise report a conflict, or require `--force`.

### S6 (L-M, python): the per-write timeout stops working once `detect()` returns
- **Where:** `proto.py:532-535`. `write_one` opens `deadline(120)` and then calls `detect()`, which nests `deadline(30)` (`:254`). The inner `finally` runs `signal.alarm(0)` and cancels the outer alarm, so `f.save()` and the verify run with no timeout.
- **Evidence:** inside `deadline(120)`, `signal.alarm` showed 120 s remaining before `detect()` and 0 after it.
- **Fix:** save and re-arm the remaining time in `deadline` (`prev = signal.alarm(0)`, then restore `max(prev - elapsed, 1)`), or use `setitimer` and keep the outer timer.
- **Report impact:** the python REPORT's "each file is wrapped in a SIGALRM deadline" is true only for reads.

### S7 (L, python + rust): no check that the original is unchanged just before the rename
- **Details:**
  - python never re-checks. It copies `path` to the temp file *after* hashing and backing it up (`proto.py:517,534`), so a concurrent change between those steps ends up in the temp file and is committed, while the backup holds older content.
  - rust checks against the preflight sha before the backup (`safe.rs:295`), but not before the rename.
  - go re-checks immediately before the rename (`write.go:412`).
- **Fix:** copy the temp file from the verified backup (or hash the temp copy), and re-hash the original just before the rename.

### S8 (L, go): `--fail-after N` rolls back even when there was no write after the Nth commit
- **Where:** `write.go:339-343`. With `--fail-after 3` on a 3-file album, all 3 commit, then the whole batch is rolled back. Python and rust only fail if there is an (N+1)th file.
- **Effect:** harmless, but E5c semantics differ between candidates. Worth aligning before comparing E5 results.

### S9 (L, go): leaks and silent errors
- **Where:** `write.go:358`. A failed `copyDurable` leaves a partial backup file behind, and its name then blocks reuse (O_EXCL).
- **Also:** `rollbackCommitted` and `cmdRollback` ignore `j.save()` errors (`:314`, `:511`), so the batch output can say `rolled_back` while the journal on disk still says `committed`.

### S10 (L, go): a timed-out library call keeps running on the temp file
- **Where:** `guarded` (`read.go:28-51`). After a `WriteTags` timeout, the goroutine keeps running while `writeOne`'s defer removes the temp file. That is harmless on Linux (unlinked inode), but the report should say that wazero calls are not cancelled.
- **Rust:** `guarded` (`fmt.rs:53`) has the same property.

---

## 2. Gaps in `harness/verify.py` (each one lets a real change pass)

| # | Gap | Evidence | Fix |
|---|---|---|---|
| V1 (H) | **Ogg Vorbis and Opus genres are never checked as native multi-value.** ffprobe joins repeated fields with `;` (`real_flac` shows `GENRE=Electronic;Ambient`), and `split_genres` splits on `;`. One `GENRE=Ambient;Electronic;Soundtrack` field is therefore indistinguishable from three fields. The metaflac check covers FLAC only. | Crafted a single-field Ogg (`vgap2-0Xdf`): `ok changed a.ogg … 0 with problems`, exit 0 | Parse the Vorbis comment packet for Ogg (stdlib: page reassembly, then the comment header), or use `ogginfo`/`opusinfo` if available, and require separate GENRE fields |
| V2 (H) | **A writer that silently skips files passes.** Files with unchanged bytes get `ok unchanged` and never count as problems (`:234-236`, `:327`). A no-op `set-genres` gives exit 0. | Unchanged FLACs printed `ok unchanged` (`vgap-jri3`) | Take the list of files that were supposed to change (or `--expect-changed DIR`) and FAIL `unchanged` for them |
| V3 (M) | **Leftover temp or backup files do not affect the exit code** (`:336-341`). `.leftover.tmp` was printed but not counted. | `vgap-jri3` | Count leftovers in `bad`, or exit non-zero |
| V4 (M) | **ID3v2 frames that ffprobe does not expose can be dropped unnoticed:** UFID (MusicBrainz track id in ID3), POPM, and likely GEOB, RVA2 and MCDI. **Extra values of multi-value text frames** are also invisible (ffprobe shows only the first). Dropping `TPE1` value 2 passed. Only PRIV was flagged. | `vgap3-U6Dm`: master had UFID+PRIV+POPM+TPE1=[JD, Second Artist]; work had none of them and TPE1=[JD] → only `id3v2_priv.x` reported | Compare a raw ID3v2 frame inventory: (id, description) → sha of the body, ignoring the target frames. The frame walker in `id3v2_genres` already does most of this |
| V5 (M) | **Picture type and description changes are not checked.** Only the payload md5 of attached-pic streams is compared. | Changed APIC type 3→4 and description; result `ok changed` | Compare the attached-pic stream tags (`comment` = type, `title` = description) and the MIME type. For FLAC, use `metaflac --list --block-type=PICTURE` |
| V6 (M) | **ID3v1 and APEv2 content is not compared.** Any ID3v1 change is accepted as "re-synced" (`:255-259`). For APE only presence is checked (`:263`). A writer that garbles the ID3v1 title or edits APE items passes. Go's report notes the lossy ID3v1 title it found by hand, and the verifier did not flag it. | Code reading; the go report (empty v1 title) confirms it in practice | Decode ID3v1 fields and require that only the genre byte or the v2-mirrored fields changed. Hash the APE item list |
| V7 (L) | File mode is not checked, although spec step 5 requires it to be preserved. | Code reading | `stat` master and work and compare `S_IMODE` |
| V8 (L) | `id3v2_genres` ignores tag-level unsynchronisation and v2.4 per-frame flags (data-length indicator, per-frame unsync). It also decodes UTF-16 (enc 1) as one stream, so later values keep a `﻿` BOM. These cause false FAILs, not false passes. | Code reading `:129-156` | Handle the flags, and decode each NUL-terminated UTF-16 value on its own |
| V9 (L, unverified) | The packet md5 (`-c copy`) does not cover the MP3 Xing/LAME frame or the MP4 `iTunSMPB`/edit lists, so gapless metadata could change without being flagged. I did not test this. | – | Optionally hash the first MPEG frame raw |

**Fixture issues (`harness/build_fixtures.py`):**
- **F1 (M):** `edge/mp3_id3v1_only.mp3` is not ID3v1-only. `-write_id3v2 0` is not an option of the mp3 muxer (`ffmpeg -h muxer=mp3` lists only `id3v2_version` and `write_id3v1`), so the file starts with a 34-byte ID3v2.4 tag holding `TSSE`: `49443304 00 00 00000022 54535345…`. The ID3v1→v2 promotion case is therefore never tested. **Fix:** use `-id3v2_version 0`. Go and Rust both noticed this; the python REPORT describes it as a "newly written ID3v2 tag", which is wrong (see C2).
- **F2 (L):** the comment says "ALAC in MP4 with freeform iTunes atoms (MusicBrainz-style)", but `m4a_alac.m4a` has no `----` atoms. Only 1 of the 3 `real_m4a` files contains one freeform atom. The reports' claims that MP4 freeform atoms are preserved rest on that single atom.
- **F3 (nit):** a comment refers to `copy_work.sh`; the script is `prepare_work.sh`.

---

## 3. Ecosystem claims (checked 2026-11-17 via the GitHub, crates.io and PyPI APIs)

| Claim | Result |
|---|---|
| mutagen 1.48.1, 2026-06-25; 1.48.0 2026-06-22; 1.47.0 2023-09; 1.46.0 2022-10 | ✅ (GitHub releases, PyPI upload 2026-06-25) |
| mutagen #748 (2026-10-31, fragmented MP4 tfhd), #754 (2026-11-13, GEOB/RVA2 dropped on load), #729 (2026-09-18, FLAC file-like save) | ✅ all open, with matching dates and titles |
| go-taglib v0.15.0 = 2026-10-12, bundles TagLib 2.1.1 | ✅ the `taglib` submodule at v0.15.0 is `7d867161…`, which equals TagLib tag `v2.1.1` |
| go-taglib #18 open "Proposal for new API"; #26 open | ✅ both open. #26 is titled *"Question: how to read id3v1 or id3v2"*. It is a read-side question, so the report's "no way to choose ID3v1 vs ID3v2" stretches it slightly (L) |
| go-taglib #15 closed 2025-06, #29 closed 2026-10 | ✅ |
| Go report: "bundled copy is **4** releases behind (2.2, 2.2.1, 2.3, 2.3.1, 2.3.2)" | ❌ (nit) that list has 5 releases, and it is 5 behind (TagLib releases: 2.2 2026-02-18 … 2.3.2 2026-09-05) |
| lofty 0.25.5 released 2026-10-25 | ✅ crates.io `2026-10-25T18:24Z` |
| lofty #784, #783 (2026-11-10), #762 (2026-10-28), #703 (2026-08-26), #685, #445 | ✅ all open, titles match. #703's body does contain the MP4 `hdlr`-destroyed and `udta`/`ilst`-dropped items |
| Not checked | star, fork and download counts; Navidrome fork usage; Arch/Debian package versions; Python/Go/Rust app-stack library versions |

---

## 4. Report claims not supported by code or results

- **C1 (M, python REPORT is stale against the current verifier).** The table still says E2 "12 ok, 3 FAIL (real_mp3)", E4 "4 FAIL (3 TCON)", and E5b "mp3 FAIL". It also has a whole "Where I think the verifier is wrong" section. The lead's re-run with the updated verifier (`$EVAL/final_python_*.json`) shows **E2 0 problems**, E3 1 (`mp3_id3v1_only`) and **E4 1** (`mp3_id3v1_only`). `results/python/e2_verify.txt` and `e4_verify.txt` are still from the old verifier. The report and those files should be updated, otherwise a reader compares stale python numbers with fresh rust numbers.
- **C2 (L, python).** The claim that "the V1 title… are promoted into a **newly written** ID3v2 tag" is inaccurate. The fixture already has an ID3v2.4 tag (F1). mutagen adds the merged v1 frames to it. The real-world ID3v1-only behaviour is untested.
- **C3 (M, python and rust).** Both reports say rollback is verified by sha256. True, but the check runs *after* the file has been replaced (S2), and neither prototype survives backup-dir reuse (S1). "Worked in every E5 scenario" holds only for the E5 scenarios as designed.
- **C4 (L, python).** The "SIGALRM deadline" protects reads only (S6).
- **C5 (L, go).** E2–E4 verifier outputs (`results/go/e*_verify.txt`, 16:27) can't be reproduced now: `$EVAL/work/go` no longer exists (the report says the go work dirs were deleted), and there is no `final_go_*.json` next to the python and rust ones. The go numbers are therefore unverifiable from the current state. The report does say the reruns used the updated verifier.
- **C6 (L, go).** "E5 all pass" plus `--fail-after` semantics: go fails and rolls back after the last commit too (S8), so its E5c is not the same experiment as python's and rust's. With N=2 on a 3-file album the outcome matches, so this is a definition issue only.
- **C7 (L, all).** The "pictures identical" and "unrelated tags preserved" claims rest on the verifier, which ignores picture type and description (V5), hidden ID3 frames and extra multi-values (V4), and ID3v1/APE content (V6). The prototypes' own snapshots cover some of this (python ID3 `_writeData`, rust `PicInfo` with type and description, go `Picture` with type and description). For MP3 and pictures the evidence is stronger than the verifier alone. For Ogg genres (V1) only the prototypes' self-checks back the "native repeated fields" claim.
- **C8 (L, rust).** "Every verifier run passes" matches `final_rust_*.json` and `results/rust/*/verify*.txt` (all 0 problems). The caveats are the V1–V6 gaps above.

No findings against the lofty L1–L8 descriptions or the go TagLib behaviour notes beyond what is listed here. I did not re-run those experiments.
