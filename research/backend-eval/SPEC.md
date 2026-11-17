# Backend evaluation: prototype specification

This is disposable research code for choosing Gama's backend technology. It is
**not** Gama. Each candidate builds the same small CLI so that the results can be
compared directly. Keep prototypes small. The point is what the *metadata
library* does, plus how hard the safety protocol is to write in that language.

| Candidate | Directory | Metadata library |
|---|---|---|
| python | `python/` | mutagen (latest from PyPI, in a venv) |
| go | `go/` | primary: `go.senan.xyz/taglib` / github.com/sentriz/go-taglib (TagLib compiled to WASM, no cgo). Briefly assess the alternatives (cgo TagLib bindings, dhowden/tag, bogem/id3v2, go-flac), and justify the choice in the report |
| rust | `rust/` | lofty (latest from crates.io) |

## Paths

- `EVAL=/tmp/claude-1000/-storage-development-gama/88084e20-e91b-48dc-a0ff-99d209baba8d/scratchpad/eval`
- Master fixtures: `$EVAL/master` (**never write here**).
- Fresh working copy: `harness/prepare_work.sh $EVAL <candidate> <run>` prints a new
  dir `$EVAL/work/<candidate>/<run>`. Use a new run dir for every experiment.
- **Never touch `/storage/music`.**
- Neutral verifier (ffmpeg/ffprobe/metaflac only):
  `python3 -I harness/verify.py genres $EVAL/master/<sub> $WORK/<sub> --genres "Ambient;Electronic;Soundtrack"`
  `python3 -I harness/verify.py fields $EVAL/master/<sub> $WORK/<sub>`
  Read `harness/verify.py` to see what it checks. If you think the verifier is
  wrong for a case, say so in your report. Do not edit it; the lead owns it.

Fixtures (`$EVAL/master`): `albums/real_flac` (3 real FLACs, repeated GENRE
fields, no PADDING block), `albums/real_mp3` (3 real ID3v2.4 MP3s with lyrics and
cover), `albums/real_m4a` (3 real AAC M4As, moov after mdat), `albums/synth_vorbis`,
`albums/synth_opus` (generated, with METADATA_BLOCK_PICTURE, MusicBrainz,
ReplayGain and a custom unicode tag), `albums/partial_failure` (mixed formats with
one truncated M4A and one read-only M4A), and `edge/` (ID3v2.3, ID3v1-only,
APEv2+ID3v1 trailer, FLAC with two pictures and no padding, FLAC with a
prepended ID3v2, ALAC, faststart M4A where moov comes before mdat, truncated
FLAC/MP3/M4A, a header-only M4A, a zero-byte file, random garbage, and FLAC data
in a `.mp3` file).

## CLI contract

Name the binary or entry point `proto`. Every command prints JSON to stdout.

1. `proto read <paths...>`: recurse into directories. For each file, output
   `{path, ok, error, format, title, artist, album, album_artist, year, track,
   track_total, disc, disc_total, genres: [..], pictures: [{type, mime, bytes,
   sha256}], other_tags: {key: [values]}, elapsed_ms}`. A malformed file must give
   `ok:false` with an error. It must never crash, panic or hang the process. Also
   record how the format was detected (extension or content).

2. `proto set-genres --genres "A;B;C" --backup-dir D --journal J.json [--continue-on-error] [--fail-after N] <files...>`:
   a bulk write that follows the safety protocol below. It replaces the full
   genre list with the given list. It writes multiple values the native way for
   each format: repeated Vorbis comment fields; multiple ID3v2.4 TCON values;
   MP4 ©gen. Document what the library actually does for each format, and for
   ID3v2.3 input in particular.

3. `proto set-fields <files...>`: writes title="Gama Title ✓", artist="Gama Artist",
   album="Gama Album", album_artist="Gama Album Artist", year=2024, track=7/12,
   disc=2/3. It uses the same safe write path (backups may go to a temp dir) and
   touches nothing else.

4. `proto rollback --journal J.json`: restores every file in the journal from its
   backup and checks the sha256 of each restored file.

## Safety protocol for writes (implement the same in every language)

1. **Preflight**: parse every target and check that it is a regular file you can
   write to. Without `--continue-on-error`, any preflight failure aborts the whole
   batch with **zero** files modified. With it, unhealthy files are skipped and
   reported. Do not modify files without write permission. Note whether the
   library or OS would have let you.
2. For each file: copy the original to the backup dir, record
   `{path, backup, sha256_before, state}` in the journal, and fsync the journal.
3. Copy the original to a temp file in the **same directory**, apply the tag
   change to the temp file with the library, then fsync.
4. Reopen the temp file with the library. Verify that the intended values are
   present and that every other tag and picture equals the pre-read.
5. Atomically rename the temp file over the original. Preserve the file mode.
   Mark the file committed in the journal.
6. On any failure without `--continue-on-error`, roll back every file already
   committed in this batch and verify the restore by sha256. With the flag,
   continue and report each failure.
7. `--fail-after N` simulates a crash or error after N commits, to test step 6.
8. Leave no temp files behind, even on failure.

If the library cannot write to an arbitrary path or file handle, which step 3
needs, document this. Then use the closest safe alternative and explain it.

## Experiments to run, with commands and outputs saved under `results/<candidate>/`

- E1 `read` over the whole working copy, saved as `read.json`. Note every file
  that fails, every crash or hang, and any wrong or missing values compared with
  ffprobe.
- E2 `set-genres` "Ambient;Electronic;Soundtrack" on each of `albums/real_flac`,
  `real_mp3`, `real_m4a`, `synth_vorbis` and `synth_opus` in one fresh run, then
  `verify.py genres` on `albums/`. Also run proto `read` again, to compare the
  library's own view.
- E3 field round trip: `set-fields` on one file of every album format plus the
  edge files you can write to, then `verify.py fields`.
- E4 `set-genres` on each writable `edge/` file separately (with
  `--continue-on-error`), then `verify.py genres` on `edge/`. Record the result
  per file, especially faststart M4A, APEv2 MP3, ID3v2-prefixed FLAC, ID3v2.3
  (does it get upgraded to 2.4? is anything lost?) and the two-picture FLAC.
- E5 partial failure: run `set-genres` on `albums/partial_failure` (a) without
  `--continue-on-error`: expect zero changes, check with sha256; (b) with it;
  (c) with `--fail-after 2` on a healthy album: expect a full rollback, check
  with sha256; (d) `rollback --journal` after a successful batch.
- E6 resources: build time, artifact size and runtime dependencies, and
  peak RSS and wall time for `read` over the working copy
  (`/usr/bin/time -v`).

## Deliverable: `results/<candidate>/REPORT.md`

Keep it concise and factual. **Record failures and limitations, not only
successes.** Include:
- library name and version, and the language and toolchain version
- an E1 to E6 result table, with verifier output excerpts
- multi-value genre behaviour per format, picture handling, and what happened to
  unknown or unrelated frames
- any audio-data change (the verifier compares decoded PCM and compressed
  packets)
- malformed-file behaviour: errors, panics, hangs, misdetection
- how hard the safety protocol was to implement (LOC, awkward APIs)
- ecosystem notes with sources and dates: library maintenance and release
  cadence, open issues relevant to tag writing, documentation, licence, adoption
- the application stack for criterion 2, from research only and without
  building it: HTTP framework, SQLite plus migrations, filesystem watching and
  scanning, background jobs, authentication, testing, and packaging for
  Arch/Debian. Name the specific libraries you would choose and say why.
