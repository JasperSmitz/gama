# ADR 0002: Genre editing policies

*Status: accepted, 2026-11-17. Context: [requirements](../requirements/bulk-genre-editing.md),
[plan](../plan/bulk-genre-editing.md) (open questions Q1–Q9), and the review rounds of M1–M8.*

## Context

Gama edits the genre metadata of files that belong to the user. Safety and correctness come before performance,
and "a reliable, limited implementation is preferable to a feature-rich application that risks corrupting the
collection". mutagen is the metadata library. It normalises some data silently, so a check that only reuses mutagen
would see its own normalisation as "unchanged".

## Decisions

### Genre model
1. **Genres are stored values, not a parsed list.** A file has a `GenreField` with:
   - `values`, exactly as stored;
   - `style`: native, delimited, mixed or empty;
   - `delimiter`, with its spacing;
   - `components`, what the user sees.
   Operations work on components and write values in the file's own convention.
2. **Only `;` is a separator** (Q4). `/` and `,` are never split, because of `AC/DC` and `R&B/Soul`.
3. **Mixed style is refused.** That is several values where one contains `;`; guessing could destroy information.
   **Delimited values are kept delimited**, with the same separator. A delimited result that would not parse back
   identically is refused (`would-not-round-trip`).
4. **Comparison key:** NFC(casefold(NFC(collapse(trim)))). Add keeps the existing spelling; remove takes away every
   case variant and duplicate. Unrelated values (duplicates, odd case, empty values, order) are kept, except that a
   remove leaving only empty values deletes the field.
5. **Replace is a separate operation.** It has its own page, a confirmation when the plan is created, a second one at
   confirm, and takes 1–20 genres. An empty replace is not allowed; remove covers deleting genres.
6. **Input validation.** Genres are rejected when they contain `;`, control characters, invisible format characters
   (zero-width joiners are allowed only inside a word), surrogates, or private-use or unassigned code points, or are
   longer than 255 characters. A bare string is never accepted as a genre list.

### Format policies
7. **ID3:**
   - loaded with `load_v1=False` and `translate=False`;
   - v2.4 stays v2.4. **v2.3 is upgraded to v2.4** (Q1), but only when Gama can prove the upgrade is lossless
     (TYER/TDAT/TIME→TDRC, TORY→TDOR, IPLS→TIPL); otherwise the file is refused;
   - `update_to_v24` runs only for v2.3 tags;
   - numeric references such as `(17)` are kept as stored, except by replace, which writes names and warns.
8. **ID3v1 trailer:** never created, never removed, kept byte-identical (Q2). mutagen's `v1=1` save rewrites it, so
   the original 128 bytes are written back. **APEv2** is never touched.
9. **MP4:** native multi-`data` `©gen` for new multi-genre values (Q3); files that already use `;` keep it. A numeric
   `gnre` becomes `©gen` with its name. Files are refused when mutagen would re-render other items differently
   (checked by rendering in memory and comparing raw bytes).
10. **Vorbis comments (FLAC, Ogg, Opus):**
    - one field per genre, in the place of the first existing genre field, with its key spelling;
    - Ogg padding is kept at its original length;
    - files with invalid UTF-8, comments mutagen would drop, or tails it would rewrite are refused;
    - FLAC without a comment block is refused.
11. **Files Gama cannot preserve exactly are refused, not "best-effort" written** (Q8). This covers symlinks, hard
    links, files not owned by the Gama user, ID3v1-only and ID3v2.2 files, FLAC with an ID3 prefix, fragmented or
    truncated MP4, footers, extended headers with CRC or restrictions, frame status flags, and dropped or merged
    frames.
12. **Format detection is by content.** mutagen classes are chosen explicitly, never by `mutagen.File` scoring.

### Verification
13. **Raw bytes, not just mutagen's view.** The snapshot used to prove "nothing else changed" includes:
    - the raw frame IDs, ID3v1 and APEv2 bytes, and the MP3 audio region;
    - raw `ilst` items, `moov` boxes (chunk offsets relative to their mdat) and the `mdat` hash;
    - the Ogg comment tail and the FLAC blocks.
    Read-time checks refuse files where mutagen would drop or normalise data.
14. **Audio:** the ffmpeg packet md5 must be identical (Q7). It runs when ffmpeg is present; otherwise the result
    records `not_checked`.
15. **mutagen runs only in short-lived child processes** with timeouts, for both reads and writes. The parent owns
    every rename.
16. **An independent oracle** (`tools/verify`, stdlib parsers plus ffmpeg, no mutagen) checks every format × operation
    in the test suite.

### Write engine and recovery
17. **Back up everything first, then verify every backup, then edit.** Backups are content-addressed, read-only and
    `O_EXCL`; they are never overwritten or deleted. They are kept for good (Q5).
18. **Per-file atomic rename; multi-file jobs are not atomic.** The default batch policy is stop-and-roll-back (Q6);
    continue-on-error is opt-in.
19. **Decide by hash, not by recorded state.**
    - After the rename, `== sha_after` means committed, whatever error happened later.
    - Rollback and recovery act on the current hash.
    - Rollback outcomes are recorded separately (`restore_state`).
    - A file that is not found becomes `missing` (its `sha_after` is kept), never `failed`.
20. **Recovery never guesses.**
    - If a library root is absent, or is not the filesystem the job wrote to (its `st_dev`/`st_ino` is recorded in the
      journal), recovery is deferred and the job is shown as "recovery pending".
    - Jobs touching those files are refused.
    - An operator can resolve it explicitly (`gama_recover --resolve`) after a re-check.
21. **A journal write failure stops the job without a rollback**, because a rollback could not be journaled.
    Recovery resolves it by hash later. A failed append is truncated and the journal is poisoned.
22. **One worker**, enforced by a process-lifetime lock, and one write job at a time (a global `flock`). A job that
    finds the write lock busy is requeued, not failed.
23. **Restore is a planned, previewed, confirmed job.**
    - The backup is verified before anything is touched.
    - A file changed after the edit is a conflict unless forced. Forcing replaces only the exact version seen in
      the restore preview (its hash is part of the plan digest; the CLI needs `--expect-sha`), backs it up first,
      and records that backup on the restore job, which can itself be undone.
    - A missing file is never recreated.
    - `gama_restore` works from the journal alone, without the database.

### Workflow
24. **Preview reads every file fresh and hashes it before and after reading.** Plans are capped at 2,000 files (Q9).
    The digest covers every field shown; confirm recomputes it from the stored rows.
25. **Only configured library roots** are listed, selected, planned or written.
26. **Authorisation:**
    - every page requires login;
    - writing and restoring require `edits.apply_edits`;
    - plans and jobs are personal (staff see all).

## Consequences

- Some legitimate files are refused (for example tags with frame status flags, or MP4 files with unusual cover
  types). The preview always says why. This is the intended trade-off.
- Hashing every file on preview and at apply is slow for big selections. That is accepted.
- The backup store only grows. Its size is shown on the History page, and pruning is a future feature.
- The ID3v1 genre may disagree with ID3v2 after an edit (Q2). v2-aware readers ignore v1.
