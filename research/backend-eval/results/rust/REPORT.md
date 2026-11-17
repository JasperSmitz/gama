# Rust candidate: lofty prototype report

*2026-11-17. This is a prototype for evaluating backends, not Gama itself. Code is in `rust/`; all experiment outputs are next to this file.*

## Summary

- **Toolchain:** Rust 1.99.1 (Arch `rust 1:1.99.1-4`). **Library:** `lofty` 0.25.5 (released 2026-10-25), MIT OR Apache-2.0.
- **Prototype:** a plain CLI binary `rust/target/release/proto` with no async and no Axum. Dependencies: `serde_json`, `sha2`, `hex`.
- **To reproduce:** from `backend-eval/`, run `sh rust/scripts/run_experiments.sh`. It writes every file in `results/rust/`.
- **Final result:** every verifier run passes. That covers 15/15 album files in E2, 15 changed files in E3, 10/10 writable edge files in E4 and E5b. Audio, packets and pictures are always identical, and no temp files are left behind. The verifier used was the lead's updated `verify.py`, with raw TCON and ilst checks.
- **That result needs workarounds.** The first, straightforward implementation failed 9 of 15 files in E2. I had to work around **four lofty write bugs or quirks** and **one damaging default**. Without them, lofty silently changes fields nobody asked it to touch:
  - `TRACKNUMBER=1/24` becomes `TRACKNUMBER=1` plus `TRACKTOTAL=24`.
  - MP4 `stik` goes from 1 to 0 as ffmpeg reads it.
  - Vorbis comments get reordered.
  - A year edit is not visible to readers.
  - A FLAC file with a leading ID3v2 tag cannot be saved.
- **lofty's own re-read could not catch any of this.** It normalises the file the same way on read, so a protocol that verifies by re-reading with the library is blind to the library's own changes. The neutral verifier is what found them.

## Library behaviour that matters

| # | Finding | Effect | What the prototype does | Reported upstream? |
|---|---|---|---|---|
| L1 | `ParseOptions::implicit_conversions` defaults to **on**. On read it splits Vorbis `TRACKNUMBER=1/24` into `TRACKNUMBER=1` + `TRACKTOTAL=24`. A save then writes the split form. | The verifier flags an unrelated `track` change on every real FLAC (`workarounds_off/verify_genres_albums.txt`). | Writes parse with implicit conversions **off** for Vorbis/FLAC/Ogg/MP4 and **on** for MPEG (see L5). | Known option (lofty issue 540 tests). The default is the trap. |
| L2 | `mp4::ilst` writes every `AtomData::SignedInteger` as **4 bytes**, whatever the original width (`write_signed_int` in `mp4/ilst/write.rs`). | 1-byte `stik=1` becomes `00 00 00 01`. ffmpeg reads it as **0** (`media_type 1 -> 0` on all 3 real M4As). Apple tools probably misread it too; I did not test that. | Before saving, re-encode `stik`, `rtng`, `akID`, `hdvd`, `shwm` and `pcst` as 1 byte and `tmpo` as 2 bytes, using `AtomData::Unknown`. | Not found in the issue tracker. |
| L3 | `VorbisComments::remove(key)` swaps matching items to the front and drains them. That **permutes the other comments**. `insert` moves the key to the end. | Unrelated comments change order (metaflac order check fails). | Order-preserving rebuild using `take_items` + `push` (`vc_set_stable`). Unit test `lofty_vorbis_remove_reorders_other_items` pins the behaviour. | Not found. |
| L4 | `Accessor::set_date` on `Id3v2Tag` inserts a **Text** `TDRC` frame. `FrameList::insert` dedups by equality, so it does **not** replace the parsed **Timestamp** `TDRC`, and the tag ends up with **two TDRC frames**. Readers, lofty included, see the old year. In memory, `date()` returns `None` right after `set_date`. | In my first E3 run, MP3 year stayed at 1979. | Remove `TDRC`/`TYER`/`TDAT`/`TIME` before `set_date`. Unit tests `lofty_set_date_duplicates_tdrc` and `id3v2_fields_replace_date`. | Not found. |
| L5 | lofty always writes **ID3v2.4**, so ID3v2.3 input is upgraded. With implicit conversions off, the v2.3-only `TYER`/`TDAT` frames are kept inside the v2.4 tag, which is spec-invalid. With them on, they merge into `TDRC=1979-06-15`. | – | Implicit conversions on for MPEG writes. | Documented behaviour. `WriteOptions::use_id3v23` exists if v2.3 should be kept. |
| L6 | FLAC with a leading ID3v2 tag: lofty documents ID3v2-in-FLAC as read-only, yet `FlacFile::save_to` still tries to save it and fails with the generic `failed to write Flac file`. | Writes to such files are impossible by default. | Drop the ID3v2 from the in-memory model before saving. lofty then writes only the Vorbis comments and leaves the ID3v2 bytes on disk untouched (verified: the file still starts with `ID3`). | Not found. |
| L7 | lofty 0.25 removed "unknown" `ItemKey`s. The generic `Tag`/`TaggedFile` API **drops items it cannot map** on conversion. | Saving through the generic API would lose custom fields. | Every read and write goes through the concrete types (`FlacFile`, `MpegFile`, `Mp4File`, `VorbisFile`, `OpusFile`). This makes the code noticeably longer (per-format `match` everywhere). | By design (doc on `VorbisComments`: "items without an ItemKey mapping will be discarded"). |
| L8 | `ParsingMode::BestAttempt` (the default) replaces undecodable text with `""`. | A read-modify-write would blank fields silently. | Writes parse in `Strict` mode. No fixture failed because of that. | By design. |

Evidence for L1/L2: `workarounds_off/` (env `PROTO_WRITE_IMPLICIT=1 PROTO_NO_MP4_INT_FIX=1`). L3 and L4 cannot be switched off any more; the unit tests in `rust/src/fmt.rs` are the record. The first E2 attempt failed 9/15 files with exactly L1+L3 (FLAC) and L2 (M4A). The MP3 failures in that run were the old verifier's ffprobe limitation.

## E1–E6

| Exp | Result |
|---|---|
| **E1 read** (`read.json`, `e1/compare_ffprobe.txt`) | 34 files, 29 ok, 5 `ok:false` (garbage.flac, zero_bytes.mp3, truncated.m4a, m4a_header_only.m4a, partial_failure/03 truncated.m4a). No panic, no hang; slowest file 0.7 ms. Detection is by content first (`Probe::guess_file_type`), with extension as fallback. `actually_flac.mp3` is detected as FLAC by content, with `mismatch:true` recorded. `garbage.flac` falls back to the extension and then fails to parse. Values differ from ffprobe only where ffprobe is the weaker reader: (a) MP3 multi-value TCON, where ffprobe shows only `Rock` and lofty shows `Rock`,`Post-Punk`, which the raw bytes confirm; (b) M4A `©gen` is literally one string `Rock;Progressive Rock`, which lofty reports as one value while the verifier splits on `;`; (c) `mp3_id3v1_only.mp3` actually contains a tiny ID3v2.4 tag (just `TSSE`) **and** an ID3v1 tag. ffprobe ignores ID3v1 when ID3v2 exists, so it shows no title. The prototype merges fields as ID3v2 → APE → ID3v1 and shows `V1 Title` etc. Durations match ffprobe within 1 s. For truncated FLAC/MP3 both report the header duration, not the real one. |
| **E2 set-genres albums** (`e2/`) | `20 files, 15 changed, 0 with problems`. Every batch exits 0. Raw TCON is `['Ambient','Electronic','Soundtrack']` in one v2.4 frame (`e2/id3_tcon.txt`). The `proto read` view after the write (`e2/read_after.json`) shows the 3 genres for every file. |
| **E3 set-fields** (`e3/`) | albums `20 files, 5 changed, 0 with problems`; edge `14 files, 10 changed, 0 with problems`. Exit 1 only because 4 corrupt edge files failed preflight under `--continue-on-error`. The first run, before the L4 and L6 workarounds, failed the 3 MP3s with ID3v2 TDRC (year stayed 1979) and the ID3v2-prefixed FLAC. |
| **E4 set-genres per edge file** (`e4/`) | `14 files, 10 changed, 0 with problems`. faststart M4A: ok, with `stco` offsets updated (packets identical) and the stik workaround applied. APEv2+ID3v1 MP3: ID3v2 TCON replaced. The APE tag has no Genre, so it is left alone and its ReplayGain items are kept. The ID3v1 genre byte is set to 26 (Ambient). ID3v2-prefixed FLAC: Vorbis genres replaced, ID3v2 bytes untouched (L6). **ID3v2.3: upgraded to v2.4**. Frames before: `TIT2 TPE1 TRCK TALB TPOS TYER TDAT TXXX TPE2 TENC TCON TXXX TSSE APIC`. After: the same, with `TYER`+`TDAT` merged into `TDRC=1979-06-15` and frames reordered. Both TXXX (`CUSTOM_UNRELATED=keep me`) and the APIC survive, and nothing was lost. Note the old TCON was `Rock/Post-Punk`: lofty does not split v2.3 `/`. Two-picture FLAC: both pictures kept (front + back, with description). ID3v1-only MP3: TCON added to the existing ID3v2 tag, ID3v1 genre byte updated. truncated.flac and truncated.mp3 **were written** because lofty parses them fine; the verifier is happy with the result. Refused in preflight: garbage, zero-byte, truncated M4A, header-only M4A. |
| **E5a** no flag (`e5/a_*`) | Preflight fails for 2 files: truncated.m4a (parse) and readonly.m4a (EACCES). The batch aborts with zero files modified; sha256 before and after is `IDENTICAL`, and no backups are made. |
| **E5b** with flag | 3 committed, 2 skipped and reported. Verifier: `5 files, 3 changed, 0 with problems`. |
| **E5c** `--fail-after 2`, real_flac | 2 commits, then the simulated failure, then both rolled back in reverse order with sha256 verified. Album sha256 equals master: `IDENTICAL`. The 3rd file was never touched; its mtime is unchanged. |
| **E5d** rollback after success | 6 files (vorbis + mp3) committed, `rollback --journal` restored all 6 and verified sha256, album is `IDENTICAL` to master. A second rollback is idempotent (`already_original` ×6). |
| **Read-only file** (`e5/x_direct_save_readonly.json`, preflight) | The OS refuses open-for-write (EACCES), and lofty's own in-place save fails the same way. **The temp+rename protocol would have succeeded** because it only needs directory write permission, which would silently defeat a 0444 mode. The preflight refuses explicitly and records both facts. |
| **E6** (`e6/`) | Clean release build of 38 crates: **6.9 s** on 16 cores (debug 4.8 s, incremental 1.6 s). Binary 2.7 MB (2.1 MB stripped). Runtime dependencies: libc and libgcc_s only. `read` over the 34-file working copy: **~10 ms wall, ~14 MB peak RSS**. `/usr/bin/time` is **not installed** on this host, so I measured with `getrusage` (`rust/scripts/measure.py`, 3 runs). |

## Format behaviour

- **Multi-value genres.**
  - Vorbis (FLAC/Ogg/Opus): repeated `GENRE=` fields. Native and correct.
  - ID3v2.4: one `TCON` with NUL-separated values (`set_genre("A\0B\0C")`). `Id3v2Tag::genres()` splits it back.
  - MP4: one `©gen` atom with 3 `data` children (`Atom::from_collection`). The verifier's raw ilst parser accepts this. Whether Apple and other players show more than the first value is untested. The legacy `gnre` atom is removed.
  - APEv2: NUL-separated, only if a Genre item already existed.
  - ID3v1: a single genre byte, set to the first genre that maps.
- **Pictures.** All pictures survived every write, byte-identical: FLAC PICTURE blocks (2-picture case included), ID3v2 APIC, MP4 `covr` and Ogg METADATA_BLOCK_PICTURE. `read` reports type, mime, description, bytes and sha256 for each.
- **Unknown/unrelated tags.** These were preserved through the concrete types: TXXX, USLT, TENC, TSSE, MusicBrainz, ReplayGain, the custom unicode Vorbis tag, APE items and MP4 freeform atoms. Through the generic API they would not be (L7).
- **Audio.** No change in decoded PCM or compressed packets anywhere, according to the verifier.
- **File size.**
  - FLAC: +1048 bytes, because lofty adds 1024 bytes of padding when it must grow a file that has none, so it rewrites the whole file.
  - MP3: shrinks by about 7.6 KB, because lofty replaces the existing large padding with 1024 bytes.
  - Ogg: about +7 KB, from re-paging the comment header.
  - M4A: 0 to +52 bytes.
  - In every case the whole file is rewritten through `FileLike` on the temp copy, which is fine for this protocol.

## Malformed files

- **Errors.** Corrupt or truncated inputs got errors, never panics. The messages are vague (`failed to parse Mp4 file`) with no offset or reason. Truncated FLAC and MP3 parse and write "successfully", with tags intact and the audio still truncated.
- **Panic and hang protection.** Each library call runs on a worker thread with `catch_unwind` and a 30 s timeout. Nothing triggered either on these fixtures. Limits:
  - A Rust **stack overflow aborts the whole process** and cannot be caught.
  - A timed-out thread keeps spinning in the background.
  - Open lofty issues show both are real: #784 "ID3v2: Stack overflow" (2026-11-10) and #783 "AAC: Infinite loop causing hang" (2026-11-10).
  - Gama's scanner should therefore parse untrusted files in a **subprocess** if "never crash or hang" is a hard requirement.
- **Misdetection.** None. Content wins over extension (`actually_flac.mp3`), and the extension is used only when content detection fails.

## Safety protocol: effort and API notes

- **Size.** `safe.rs` is about 440 non-blank, non-comment lines; `fmt.rs` is about 880 plus 90 lines of tests. The per-format code in `fmt.rs` is the bulk, mostly because of L7 (no generic lossless API) and the workarounds.
- **Step 3 is native.** `AudioFile::save_to(&mut File, WriteOptions)` writes to any file handle, so the temp-copy-in-the-same-directory flow needs no compromise.
- **Easy parts.** fsync of file and directory, atomic `rename`, mode preservation, the `Drop` guard for temp files, and `serde` for the journal were all simple. Result types made the rollback paths explicit.
- **Awkward parts.**
  - Verifying with lofty itself is not independent (see the summary). I compare a full canonical snapshot of the edited original against the re-read copy. That caught an ordering issue, but not L1/L2/L4, which were masked.
  - `Frame` is not reachable through constructors like `Frame::text` (crate-private). You need `TextInformationFrame::new(FrameId::new(..)?, ..)`.
  - The `Accessor` setters behave differently per format (L3, L4).
- **Not preserved by temp+rename.** Owner and group (unless the process runs as the owner), xattrs and ACLs, hard links, and mtime, which changes. Gama would need `fchown` and `xattr` handling if those matter.
- **Verifier note.** The original `verify.py` used ffprobe, which showed only the first ID3v2.4 TCON value, so it wrongly failed correct MP3 writes. The lead's update with raw TCON and ilst parsing fixes that. I confirmed it independently with `rust/scripts/id3_tcon.py`.

## Ecosystem: lofty

Sources: GitHub API and crates.io API, queried 2026-11-17.

- **Repository and adoption.** github.com/Serial-ATA/lofty-rs, created 2021-04. 381 stars, 85 forks. Mainly one maintainer (Serial-ATA, 1729 commits; the next contributor has 118). 152 reverse dependencies on crates.io, 1.6 M total downloads (736 k recent).
- **Cadence.** Active. 0.22.4 (2025-04), 0.23.0 (2026-02), 0.24.0 (2026-04), 0.25.0 (2026-08), then 0.25.1–0.25.5 (2026-08 to 2026-10-25). Pre-1.0: minor versions break the API (0.25 removed unknown `ItemKey`s, for example).
- **Open issues relevant to writing.**
  - #762: an APE tag at the end of an MPEG file is read as ID3v1 and **overwritten on save**.
  - #685: a stray APE footer discards an entire MPEG file.
  - #784: stack overflow.
  - #783: AAC hang.
  - #703: an autonomous bug-scan batch, including MP4 write-path bugs that can destroy `hdlr` or drop `udta`/`ilst` on crafted files.
  - #445: "Make use of padding when writing".
  - L2, L3, L4 and L6 above do not appear in the tracker; they are worth filing.
- **Documentation.** Good rustdoc on docs.rs with per-format notes. The pitfalls above (implicit conversions, generic API lossiness) are documented but easy to miss.
- **Licence.** MIT OR Apache-2.0.

## Application stack for criterion 2 (research only, not built)

Versions and dates are from crates.io on 2026-11-17.

- **HTTP:** `axum` 0.8.9 on `tokio` 1.54 with `tower-http` 0.7 (static files, compression, tracing). This is the de-facto standard and it composes with tower middleware. Add `utoipa` 6.0 if an OpenAPI spec is wanted.
- **SQLite and migrations:** `rusqlite` 0.43 (2026-11-15, bundled SQLite, so there is no system-library drift on Debian) with `rusqlite_migration` 2.7 (embedded SQL migrations, using `user_version`). Run it on a small blocking pool or a dedicated writer thread (single writer, WAL). `sqlx` 0.9 is the alternative if async and compile-checked queries are preferred, but its compile-time database requirement adds friction.
- **Filesystem watching and scanning:**
  - `notify` 8.2 with `notify-debouncer-full` 0.7 (inotify on Linux).
  - `walkdir` 2.5 (stable but last released 2024-03), or `jwalk` 0.9 for a parallel initial scan.
  - Always run a periodic full rescan as a backstop, because inotify can overflow and does not see changes on NFS/SMB.
- **Background jobs:** plain tokio tasks plus a SQLite-backed job table (tag writes, rescans) are enough and keep everything in one database. `apalis` 1.2 has a SQLite backend but is small (477 k recent downloads). Tag writes should run in `spawn_blocking`, or in a subprocess per the malformed-file note.
- **Authentication:** `tower-sessions` 0.15 (SQLite store) plus `password-auth` 2.0 / `argon2` 0.6. `axum-login` 0.18 was last released 2025-07, so it is optional, and the hand-rolled version is small.
- **Testing:** `cargo test`, `insta` 1.49 snapshots for JSON and tag dumps, `tempfile` 3.27, and `axum`'s `tower::ServiceExt::oneshot` for HTTP tests without a socket. A fixture corpus plus the neutral ffprobe/raw verifier should become an integration suite.
- **Packaging:** a single static-ish binary with no runtime dependencies beyond libc. Build Debian packages with `cargo-deb` 3.8. For Arch, a standard `PKGBUILD` using `cargo build --frozen --release`, following the Arch Rust package guidelines. Add a systemd unit and `DynamicUser` or a dedicated user.

## Files

- `read.json`: E1 output.
- `e1/` to `e6/`: per-experiment command outputs, journals, verifier text and JSON.
- `workarounds_off/`: lofty-bug evidence.
- `rust/scripts/run_experiments.sh` (driver), `compare_ffprobe.py`, `id3_tcon.py` and `measure.py`.
- Work dirs kept for re-verification: `$EVAL/work/rust/{e2,e3,e4,e5b,woff}`. All other work and backup dirs are deleted. Re-running the script recreates them (about 2.8 GB on `/tmp`).
