# Go candidate: results

Date: 2026-11-17. The code is in `go/` (`main.go`, `read.go`, `write.go`, `proto_test.go`, `cgoprobe/`, `scripts/`).
Raw outputs are in this directory. `$EVAL` = `/tmp/claude-1000/-storage-development-gama/88084e20-…/scratchpad/eval`.

## Stack under test

| | |
|---|---|
| Language / toolchain | Go 1.27.4 linux/amd64 |
| Library | `go.senan.xyz/taglib` **v0.15.0** (2026-10-12), LGPL-2.1. It bundles **TagLib 2.1.1** compiled to WASM (388 KB `taglib.wasm`, embedded) and runs it on wazero. `go.mod` pins wazero to an untagged pseudo-version (`v1.11.1-0.20260428…`). |
| Upstream TagLib | 2.3.2 (2026-09-05). The bundled copy is 5 releases behind (2.2, 2.2.1, 2.3, 2.3.1, 2.3.2). |
| cgo probe | system TagLib 2.3.2 through the C API `tag_c` (`go/cgoprobe`, about 70 lines) |
| Checks | `gofmt -l` clean, `go vet ./...` clean, `go test ./...` passes (3 unit tests on the verification and parsing helpers). There is no golangci-lint or staticcheck on this machine. |

## Result table

| Exp | Result | Notes |
|---|---|---|
| E1 read | **34 files, no crash, panic or hang.** 28 ok, 6 `ok:false`. | All ok files match ffprobe on title, artist, album, album_artist, year, track and disc numbers and totals, picture count and genres. The only mismatch is ffprobe showing only the first ID3v2.4 TCON value (an ffprobe limitation). `e1_vs_ffprobe.txt` |
| E2 set-genres albums | 15/15 committed. Each album verified separately with `--require-changed`: **6 ok, 9 FAIL.** real_mp3 3/3 ok, real_m4a 3/3 ok, real_flac 0/3, synth_vorbis 0/3, synth_opus 0/3. | All 9 failures are the same TagLib behaviour: Vorbis comment keys are upper-cased (`encoder=` → `ENCODER=`). FLAC also moves the GENRE fields to the end. Genre values, audio and pictures are correct everywhere. The library's re-read shows exactly the three genres for all 15. `e2_verify.txt`, `e2_verify_<album>.json`, `e2_reread.json` |
| E3 set-fields | 14 committed, 6 refused at preflight (unreadable). **12 ok, 2 FAIL.** | `edge/mp3_id3v23.mp3`: ID3v1 tag added. `edge/truncated.mp3`: ID3v1 tag added, and because it lands after a partial frame the packet md5 changes. `e3_verify.txt` |
| E4 set-genres edge | 9 committed, 5 refused at preflight. **4 ok, 5 FAIL.** | 3 FLACs fail on comment key case and order. `mp3_id3v23.mp3` fails because an ID3v1 tag was added. `truncated.mp3` fails because an ID3v1 tag was added and the packet md5 changed. Faststart M4A, ALAC, the ID3v1-only MP3 and the APEv2 MP3 pass. `e4_verify.txt` |
| E5 partial failure | **All pass.** | (a) 0 files modified, sha256 identical. (b) 3 committed and 2 skipped (truncated, read-only). (c) `--fail-after 2`: 2 rolled back, sha256 identical. (d) `rollback --journal`: 3 restored, sha256 identical. Extras: an injected verify failure rolls back with no temp leftovers, and after SIGKILL mid-write, `rollback` removes the orphan temp file. `e5.txt`, `e5_fault_injection.txt` |
| E6 resources | build 3.0 s cold, 0.03 s incremental. Binary 10.4 MB (6.8 MB static and stripped). | Read of 34 files: **0.35 s, 39 MB RSS cold; 0.23 s, 24 MB RSS warm.** GNU `time` is not installed, so the figures come from getrusage (`go/scripts/measure.py`). `e6_build.txt`, `e6_time_read.txt` |

Verifier excerpts (strengthened verifier, fresh work dirs `$EVAL/work/go/e2`, `e3`, `e4`, kept; reproduce with `go/scripts/e234.sh $EVAL`):

```
E2 real_flac:    FAIL x3  - metaflac: non-genre comments differ (order or content)
E2 synth_vorbis: FAIL x3  - Ogg non-genre comments differ: -['encoder=Lavc63.1.102 libvorbis'] +['ENCODER=Lavc63.1.102 libvorbis']
E2 synth_opus:   FAIL x3  - Ogg non-genre comments differ: -['encoder=Lavc63.1.102 libopus'] +['ENCODER=Lavc63.1.102 libopus']
E2 real_mp3:     ok x3    . note: existing ID3v1 tag re-synced from new values
E2 real_m4a:     ok x3
E3: FAIL edge/mp3_id3v23.mp3   - ID3v1 tag added (outside requested change)   . note: ID3v2 version 2.3 -> 2.4
    FAIL edge/truncated.mp3    - compressed audio packets md5 differs: MD5=9374f113… -> MD5=dc74d32e…
                               - ID3v1 tag added (outside requested change)
    34 files, 14 changed, 2 with problems
E4: FAIL flac_two_pictures_no_padding.flac / flac_with_id3v2_prefix.flac / truncated.flac (comment order/case)
    FAIL mp3_id3v23.mp3  - ID3v1 tag added; notes: TYER 1979 + TDAT 1506 -> TDRC 1979-06-15
    FAIL truncated.mp3   - packet md5 differs; ID3v1 tag added
    ok   m4a_alac, m4a_faststart, mp3_id3v1_only, mp3_id3v24_apev2_id3v1
    14 files, 9 changed, 5 with problems
```

## Format behaviour (what TagLib actually does)

**Multi-value genres.** All three cases come from go-taglib `WriteTags(path, {"GENRE": [...]}, 0)`, which TagLib applies as `PropertyMap.replace` and then `setProperties`.
- **FLAC, Ogg Vorbis, Opus:** writes repeated `GENRE=` fields (native). **However, TagLib rewrites the whole Vorbis comment block.** Every field name is upper-cased (`encoder=…` becomes `ENCODER=…`), because TagLib keys its field map by upper-case name, so original case is lost on *any* save, for FLAC, Ogg Vorbis and Opus alike. In FLAC the replaced key also moves to the end. This is semantically harmless (Vorbis field names are case-insensitive) but byte-visible, and it is the cause of all 9 E2 failures and the 3 FLAC failures in E4. The vendor string is kept.
- **FLAC padding:** TagLib added a 4096-byte PADDING block to FLACs that had none. The audio frames are unchanged (the PCM and packet md5s match).
- **MP3 (ID3v2.4):** one TCON frame `Ambient\0Electronic\0Soundtrack` (native 2.4). The TCON frame moves to the end of the frame list, and the tag padding absorbs the change, so file size is unchanged. USLT, APIC, TXXX and TENC are kept.
- **M4A:** one `©gen` atom holding **three `data` children**. This is TagLib's convention. ffmpeg joins them with `;`, but iTunes and many players read only the first. Existing `Rock;Progressive Rock` was one string and stays one value on read.
- **ID3v2.3 input:** **always upgraded to ID3v2.4** on save. go-taglib exposes no version option. TYER+TDAT `1979`/`1506` became TDRC `1979-06-15` (correct merge in 2.1.1, while TagLib 2.3.2 through cgo printed "TDAT … will be discarded"). The 2.3 single string `Rock/Post-Punk` was replaced by native 2.4 multi-values. Text frames were re-encoded. TXXX `CUSTOM_UNRELATED`, TXXX `lyrics-eng`, TSSE and APIC were kept. I found no other frames lost.

**MP3 side effects (TagLib's default `MPEG::File::save()` = AllTags + Duplicate):**
- **TagLib creates an ID3v1 tag on every MP3 it saves**, appending 128 bytes after the audio. On `truncated.mp3` those bytes follow a partial frame, so the decoder consumes them and the packet md5 changes. That is the only "audio change" seen. It is not caused by the audio frames being rewritten.
- The ID3v1 copy is lossy. Title `Gama Title ✓` (not Latin-1) was written as an **empty** ID3v1 title. The genre byte was rewritten (17 Rock → 26 Ambient). On the APEv2+ID3v1 file the existing ID3v1 values were overwritten the same way.
- The **APEv2** tag (`REPLAYGAIN_TRACK_GAIN`, `MP3GAIN_MINMAX`) was preserved byte-for-byte. But it is **invisible to the library's read** (the ID3v2 tag wins in TagLib's tag union), so my verify step cannot check it, and a Gama genre edit would never sync it.
- `edge/mp3_id3v1_only.mp3` is not really ID3v1-only: it has a 34-byte ID3v2.4 tag holding only TSSE. Because of it, TagLib *and* ffprobe both ignore the ID3v1 title and artist on read. The **fixture's name is misleading**, and the lead may want to fix it.
- The cgo probe against TagLib 2.3.2 shows the same ID3v1 append and the same v2.3→v2.4 upgrade. Avoiding the append needs the C++ call `MPEG::File::save(ID3v2, StripNone, v4, DoNotDuplicate)`. Neither go-taglib nor `tag_c` exposes it. It is doable with a custom WASM shim (go-taglib supports `-ldflags -X …binaryPath=`) or a cgo C++ shim.

**Pictures.** Every picture (type, MIME, description, sha256 of the bytes) was identical before and after on all writes, including the two-picture FLAC (both kept, in order) and the ID3v2-prefixed FLAC. The verifier agrees. Reading picture bytes costs one full file open and parse per picture (`ReadImageOptions(path, i)`), because go-taglib has no handle API.

**Unknown or unrelated tags.** Tags visible in the property map (MusicBrainz, ReplayGain, custom unicode tag, lyrics, TXXX) were preserved, and the verifier confirms. go-taglib does **not** expose `PropertyMap::unsupportedData()` or raw frames, so frames TagLib cannot map are kept by TagLib but are not visible to my post-write verification. Examples: APE items behind ID3v2, unknown MP4 atoms. The verifier is the only check on those.

**Other container notes.** Faststart M4A: moov stays before mdat, grows by 698 B, chunk offsets are updated, and the audio md5s match. ALAC: fine. A FLAC with an ID3v2.3 prefix keeps the prefix but **upgrades it to 2.4** (+1 KB).

## Malformed files (E1)

| File | Result |
|---|---|
| `truncated.m4a`, `m4a_header_only.m4a`, `garbage.flac` | `ok:false`. **go-taglib returns no error**: `ReadProperties` returns a zero `Properties{}` with `err == nil` when TagLib can't open the file. My code has to detect `Format == ""` itself. `ReadTags` does return `ErrInvalidFile`. |
| `zero_bytes.mp3` | **TagLib "opens" it as a valid MPEG file** with no stream. My code turns sample_rate 0 into `ok:false`. |
| `actually_flac.mp3` | **Misdetection.** TagLib picks the parser by extension and does *not* fall back to content sniffing. It scans the whole 2 MB file for MPEG sync (166 ms), finds none, and reports an empty MPEG file. The tags (`Key`, genres) are lost to the reader. My code reports `ok:false` plus `by_content: flac`. Through cgo, `taglib_file_new_type(FLAC)` reads it correctly. go-taglib has no way to force the type: it is path-only, so it would need a copy or symlink with the right extension. |
| `truncated.flac`, `truncated.mp3` | read ok (the tags are intact), and writable. |
| No panics, hangs or crashes on any fixture. | Every library call still runs in a goroutine with recover and a 30 s timeout. **A hung WASM call cannot be cancelled**: go-taglib uses `context.Background()` and does not enable wazero's close-on-context-done, so a timeout only abandons the goroutine. Earlier versions also had host crashes: issue #15, SIGSEGV with wazero 1.8.2, fixed by a wazero bump. |

Detection method recorded per file: `by_extension`, `by_content` (my own magic-byte sniff), the library's format/codec, and `method`.

## Safety protocol: how hard it was

- About 290 non-blank lines for protocol and journal (`write.go`), about 210 for read and detection, plus 100 for the CLI. That is about 1000 lines including the tests and comments. It was a straightforward write, and the Go standard library has everything (`os.CreateTemp`, `File.Sync`, `os.Rename`, `x/sys/unix.Access`).
- **Path-only API.** go-taglib cannot write to a file handle or `io.ReadWriteSeeker`. It mounts the file's *directory* into the WASM sandbox (read-write for writes) and opens by path. Step 3 still works: I copy to a temp file in the same directory and let the library edit the temp in place. **The temp name must keep the original extension** (`.<stem>.gamatmp-XXXX.m4a`), because TagLib chooses its parser by extension.
- `WriteTags` returns only `ErrSavingFile` ("can't save file") with no reason. There is no in-memory write: `tag_c`'s memory iostream exists in TagLib but go-taglib doesn't use it.
- **Read-only file:** a direct `taglib.WriteTags` on a 0444 file fails (TagLib opens it read-only), so the library and OS refuse. But **`rename(2)` over the 0444 file succeeds**, because only directory write permission is needed. The temp+rename protocol therefore *would* modify read-only files without the explicit preflight `access(W_OK)` check, which is the only guard. (`e5.txt` (e))
- Verification needs one `ReadTags` + one `ReadProperties` + one `ReadImageOptions` per picture, each a fresh WASM instance and file parse.
- Also implemented: the journal is written atomically each step (tmp + fsync + rename + dir fsync); the backup's sha256 is checked; the original's sha256 is rechecked just before rename (refusing if it changed concurrently); file mode is preserved with chmod before rename. Ownership and xattrs are **not** preserved.
- `--fail-after` is treated as a fatal error that always rolls back, even with `--continue-on-error`.
- Side effect: wazero writes a 2.4 MB compilation cache to `$TMPDIR/go-taglib-wasm-<uid>`. A cold start costs about 110 ms and 15 MB more RSS.

## Alternatives assessed (briefly)

| Option | Verdict |
|---|---|
| **go-taglib (WASM)** (chosen) | Full TagLib format coverage, multi-value tags, pictures, no cgo, static cross-compiled binary. Downsides: lags upstream TagLib; path-only, stateless API (re-parses per call); no save options; no error detail; no raw or unsupported frame access. Navidrome uses it, but through a **fork** (`deluan/go-taglib` via `replace`; issue #18 proposes a handle API). |
| cgo + system TagLib (`tag_c`) | Tested: works. Same TagLib write behaviour (key upper-casing, ID3v1 append, v2.4 upgrade). Gains: current TagLib 2.3.2, `taglib_file_new_type` (fixes misdetection), a file handle (one parse), memory iostream. Costs: cgo, a dynamic libtag dependency, no cross-compilation. The existing Go wrappers (`wtolson/go-taglib`, last push 2021-04) are stale and basic-fields only, so you'd write your own (about 70 lines for the probe). Full control (save flags) needs a C++ shim. |
| `dhowden/tag` | Read-only (BSD-2). Last push 2024-07-30, 34 open issues. Not viable for writing. |
| `bogem/id3v2` (now `n10v/id3v2`) | ID3v2 only (writes v2.3/v2.4, MIT). Last release v2.1.4 (2023-02-09), last push 2023-10. Would need separate FLAC/Ogg/MP4 libraries. Effectively unmaintained. |
| `go-flac/go-flac` + `flacvorbis` | FLAC only, Apache-2.0, small (45 and 13 stars). Last push 2025-08 and 2024-08. Preserves raw blocks (good for byte fidelity), but there is no maintained Go MP4 tag writer to pair it with. |

**Why go-taglib:** it is the only pure-Go option that writes all five formats with native multi-values. Its weaknesses (ID3v1 append, Vorbis key case, path-only API) are TagLib-level or wrapper-level, and could be fixed with a custom shim, which go-taglib's `binaryPath` override supports. Expect to maintain a fork, as Navidrome does.

## Ecosystem (checked 2026-11-17 via GitHub API)

- `sentriz/go-taglib`: created 2024-09. 96 stars, 5 open issues, last push 2026-11-16. Tags v0.4.0 (2024-12) to v0.15.0 (2026-10-12), roughly monthly, all 0.x (no API stability promise). Single maintainer (also maintains gonic and wrtag). LGPL-2.1: fine for a separately distributed binary that embeds it, but check the relinking obligations for static distribution.
  Relevant issues:
  - #29 (closed 2026-10): non-UTF-8 Vorbis comments made read *and* write fail.
  - #15 (closed 2025-06): SIGSEGV crashes fixed by a wazero bump.
  - #26 (open): no way to choose ID3v1 vs ID3v2.
  - #18 (open): proposal for a file-handle API.
- About 95 public go.mod files reference it (GitHub code search), including navidrome (via fork), gonic and wrtag.
- TagLib: 1.5k stars, 30 open issues, releases 2.2 (2026-02) to 2.3.2 (2026-09). Active.
- wazero: active (v1.12.0, 2026-05-29). go-taglib pins an untagged commit.
- Documentation: a short README plus pkg.go.dev. The behaviour of the property mapping has to be read from TagLib's docs and C++ source.

## Application stack for criterion 2 (research only, not built)

- **HTTP:** stdlib `net/http` (Go 1.22+ method and path patterns) plus `go-chi/chi` v5 (v5.5.0, 2026-10-14; also used by Navidrome) for middleware grouping, plus `html/template` or a JSON API.
- **SQLite:** `ncruces/go-sqlite3` (MIT, v0.37.0, 2026-11-07). It is WASM on wazero, the same runtime as go-taglib, so it adds no cgo and keeps the static binary. Alternatives: `modernc.org/sqlite` (pure-Go transpile, on GitLab) or `mattn/go-sqlite3` (cgo, the most proven, v1.14.53). Queries: `sqlc` (v1.31.1). **Migrations:** `pressly/goose` v3 (v3.30.1, 2026-11-12) with `embed.FS`.
- **Filesystem watching and scanning:** `fsnotify` (v1.10.1, 2026-05; inotify, **not recursive**, so add a watch per directory and mind `max_user_watches`), plus `filepath.WalkDir` for full scans, plus a periodic mtime/size rescan as the source of truth. Debounce events.
- **Background jobs:** in-process worker goroutines with a SQLite-backed queue (`maragudk/goqite` v0.4.0, 2026-02; or a hand-rolled jobs table). River and similar are Postgres-only.
- **Authentication:** `alexedwards/scs` v2 sessions (v2.9.0, 2025-07) with its SQLite store, plus argon2id from `golang.org/x/crypto/argon2`, plus CSRF protection via `http.CrossOriginProtection` (Go 1.25+).
- **Testing:** stdlib `testing`, `httptest`, and `rogpeppe/go-internal/testscript` for CLI and golden-file tests. Use fixtures like these, plus an external verifier, in CI.
- **Packaging:** one static binary (`CGO_ENABLED=0`, 6.8 MB here), a systemd unit, and `goreleaser/nfpm` (v2.49.0) producing `.deb` and Arch `.pkg.tar.zst` from one config, or a hand-written PKGBUILD for the AUR. With the cgo-TagLib path you would instead depend on the `taglib` package.

## What failed or was skipped

- The verifier fails on FLAC comment key case and order (TagLib behaviour, not fixable through go-taglib's API) and on `truncated.mp3` (ID3v1 append). These failures are real, and I don't think the verifier is wrong on them.
- The strengthened verifier now flags the added ID3v1 tag (on `mp3_id3v23` and `truncated.mp3`). Where an ID3v1 tag already existed it only notes "re-synced". It does not flag that the re-synced ID3v1 title is **empty** for `Gama Title ✓` (not Latin-1), which I found by inspecting the bytes.
- GNU `time` is not installed, so the measurements use a getrusage wrapper.
- golangci-lint and staticcheck are not installed; only `go vet` was run.
- A shared `/tmp` quota (EDQUOT) interrupted one run. The E2–E4 work dirs from the rerun are **kept** at `$EVAL/work/go/{e2,e3,e4}`, with backups and journals in `$EVAL/side/go-e234`. The E1 and E5 work copies were deleted.
- I did not build a custom WASM shim to avoid the ID3v1 append or keep ID3v2.3. It looks feasible but is untested.
