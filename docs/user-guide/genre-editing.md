# Editing album genres in Gama

Gama can add, remove or replace the genres of whole albums. Nothing is written until you have seen a preview of
every file and confirmed it. Every changed file is backed up first, and every edit can be undone.

This guide covers the workflow, what Gama will and will not do to your files, and how to recover from mistakes
and crashes.

## 1. Before you start

- Gama runs as two processes. Both must run as **the user that owns the music files**: Gama refuses to write files
  it does not own.
  - `manage.py runserver` (or the web service) serves the pages.
  - `manage.py gama_worker` does all writing. It must be running for edits to happen, and only one worker can run
    at a time.
- Index the library with `manage.py gama_scan`. Run it again after you change files outside Gama.
- Applying and restoring edits needs the `edits.apply_edits` permission. Grant it with
  `manage.py gama_grant USERNAME`. Other users can browse and preview, but cannot write.
- Plans and jobs are personal: you see your own, and staff accounts see everyone's.

## 2. The workflow

**Select albums → choose an operation → preview → confirm → the worker applies it → review the results.**

1. **Select albums.** On *Albums*, search by album, artist or folder and tick the albums you want.
   - Albums with the same title, editions, and multi-disc sets are listed separately with their folder, disc
     count, formats and MusicBrainz release, so you can tell them apart.
   - A multi-disc album (in `CD1/`/`CD2/` folders or tagged with disc numbers) is one album.
   - **inconsistent** means the tracks of one folder disagree on album, album artist or genres. Each combination
     is listed separately.
   - The selection stays visible on every page.
2. **Choose an operation** under *Edit genres*.
   - **Add a genre**: adds it to every track that does not have it yet. Existing genres are kept.
   - **Remove a genre**: removes it, in any capitalisation and including duplicates, from every track. Other
     genres are kept. If no genre is left, the genre field is deleted.
   - **Replace genres** is on its own page and needs an extra confirmation. It sets the genres of every track to
     exactly your list (1–20 genres).
   - Genres are compared case-insensitively, after Unicode normalisation and whitespace clean-up. Adding `rock`
     to a track that has `Rock` changes nothing; the existing spelling wins.
   - A genre may not contain `;`, control characters or invisible characters, and is at most 255 characters long.
3. **Preview.** Gama reads every file fresh from disk and shows, for every file:
   - the genres now, exactly as stored, and the genres after the edit;
   - the action: *change*, *no change* or *skipped*, with a reason;
   - warnings, for example "ID3v2.3 tag will be saved as ID3v2.4", or that an APEv2 tag holds a genre Gama does
     not edit.
   Counts of changed, unchanged and skipped files are at the top. Nothing has been written at this point.
4. **Confirm.** Click *Apply N change(s)*.
   - By default, Gama stops at the first failure and restores the files it already changed (**stop and roll
     back**).
   - Tick *continue* to keep going past failures instead.
   - When the job ends, an *Outcome* line on the job page counts the files changed, changed and rolled back,
     failed, and not attempted (the files after a stop).
   - A plan can be confirmed once, and only exactly as it was shown.
5. **The worker applies it.** The job page refreshes every two seconds and shows progress. If it says
   *Worker not running*, start `manage.py gama_worker`.
6. **Results.** For every file the job page shows:
   - the original genres, the requested ones, and the genres **read back from disk** after the write;
   - the state, the verification result, the backup location and the file hashes;
   - the full original metadata snapshot.
   The index is updated from what was read back.

### If files changed after the preview

If a file changed on disk between the preview and the write, the job ends with **conflict** and writes nothing.
Click *Refresh preview* to plan again from the files as they are now.

## 3. What Gama does to a file, and what it never does

For each file, in order:

1. Gama copies the file into a backup store. The copy is named by its content hash, read-only, and never
   overwritten or deleted. It is verified before anything is edited.
2. It makes a temporary copy next to the original, filled **from the verified backup**.
3. It edits only the genre on that copy, in a separate process with a timeout.
4. It verifies the copy:
   - the genres read back are exactly what was requested;
   - every other tag, picture and trailer is unchanged;
   - the audio packets are identical (ffmpeg).
5. It gives the copy the original's group, mode and extended attributes (ACLs).
6. It checks that the original has not changed meanwhile, then swaps the copy in with an atomic rename.
7. It hashes the result once more and records it in the job's journal.

Gama never re-encodes audio, and never renames, moves or deletes your files. It does not keep the modification time
(on purpose, so music servers notice the change).

**What is atomic and what is not.**
- Each file is replaced atomically: other programs see either the old file or the new one.
- **A job with several files is not atomic.** While it runs, others can see some files changed and some not. If
  something fails, the default policy restores the changed files from their backups.
- Durability depends on the filesystem honouring `fsync`. Network and FUSE filesystems may not.
- Gama cannot lock out other programs. A program that writes a file during the split second between Gama's last
  check and the rename can lose that write.

### Files Gama refuses to write

These files are shown in the preview as *skipped*, with the reason. They are never written:

- **Format:** an unsupported format (WAV, AIFF, WMA, APE, WavPack, …), or an unreadable or truncated file.
- **ID3 tags:**
  - content that does not match the extension;
  - an MP3 with only an ID3v1 tag, or an ID3v2.2 tag;
  - ID3 features mutagen would drop: a footer, an extended header with CRC or restrictions, frame status flags,
    frames it cannot parse or would merge, a v2.3 tag whose upgrade to v2.4 would lose frames;
  - an ID3v1 tag in an unusual place.
- **FLAC and Vorbis comments:**
  - FLAC preceded by an ID3 tag, or FLAC without a comment block;
  - text that is not valid UTF-8, or comments that mutagen would drop or rewrite.
- **MP4:**
  - a fragmented file;
  - a numeric genre atom it cannot parse;
  - other tags that mutagen would rewrite differently.
- **Genre values:**
  - genres that mix separate values with `;`-joined values;
  - a `;`-joined value that would not read back identically after the edit.
- **Filesystem:**
  - symlinks, files with several hard links, files you do not own;
  - files outside a configured library root, or below a symlinked folder.

### Format conventions Gama keeps

- **FLAC, Ogg, Opus:** one `GENRE` field per genre. The existing key spelling and the position of the fields
  are kept.
- **MP3:**
  - one ID3v2.4 `TCON` frame with several values;
  - a v2.3 tag is upgraded to v2.4 (lossless; dates become `TDRC`), with a warning;
  - an ID3v1 trailer stays byte-identical, so old players may show an older genre;
  - APEv2 tags are never touched.
- **MP4/M4A:**
  - one `©gen` atom with one value per genre;
  - a numeric `gnre` atom is replaced by `©gen` with the same name.
- **Delimited genres:** a single value like `Rock;Progressive Rock` stays a single value with the same separator.
  Only `;` is a separator; `/` and `,` are part of the genre (`AC/DC`, `R&B/Soul`).

## 4. Undoing an edit (restore)

1. Open the job under *History* and use **Restore from backups**: all changed files, or only the ones you tick.
2. Gama builds a **restore preview**, and nothing is written yet:
   - **restore**: the file still holds Gama's edit and will be replaced by its backup;
   - **nothing to do**: the file is already the original;
   - **conflict**: the file changed after Gama's edit (someone else edited it), is missing, or its backup is
     damaged. It is **not touched**.
   To overwrite later changes anyway, tick *force*. Force replaces **exactly the version shown in the preview**: that
   version is backed up first, and if the file changes again before the restore runs, it is left alone and reported
   as a conflict (build a new preview).
3. Confirm. The worker restores the files with the same safety steps as an edit. The result page shows the genres
   read back, and the index is updated.

A restore can be repeated; files that are already the original are left alone.

### Undoing a forced restore

The job page of a forced restore shows, per file, where the replaced version was kept
(`<data_dir>/backups/sha256/ab/<sha>`). Use **Undo this restore** on that page to bring those versions back, again
through a preview.

Without the web interface, copy the backup over the file by hand. The backup file is read-only, so copy its content
rather than moving it:

```sh
cp <data_dir>/backups/sha256/<first two hex digits>/<sha> "<library root>/<file>"
manage.py gama_scan
```

`cp` keeps the destination's owner and mode. Do this only while Gama's worker is idle.

### Restoring without the web interface

If the web UI or the database is not available, use the command line. It works from the job's journal alone:

```sh
manage.py gama_restore JOB_ID --dry-run         # what would happen, per file
manage.py gama_restore JOB_ID                   # restore every file the job changed
manage.py gama_restore JOB_ID --items 0,2       # only some files (numbers from --dry-run)
manage.py gama_restore JOB_ID --force --expect-sha 1:<sha>
                                                # replace exactly the version --dry-run showed for item 1
                                                # (it is backed up first; anything else is left alone)
manage.py gama_restore RESTORE_JOB_ID           # undo a forced restore (bring the replaced versions back)
manage.py gama_scan                             # afterwards: refresh the index
```

The job id is shown on the job page (with or without dashes). The journal files are in
`<data_dir>/journal/<job id>.jsonl`. The backups are in `<data_dir>/backups/sha256/`.

## 5. Crashes and interruptions

Every step of a job is written to its journal before it happens.

- **Interrupted jobs.** If the worker or the machine stops during a job, the next start of the worker runs
  **recovery**. Recovery:
  - removes Gama's temporary files;
  - decides the state of every file **by its content**: the edit is in place (*committed*), the original is in
    place (*failed*: nothing was written), the file is missing (*missing*), or the file matches neither (*conflict*);
  - marks the job **interrupted**.
  Recovery never continues a job. Review the job page and restore the committed files if you want the originals
  back.
- **A failed file in the middle of a job.** With the default policy, the files already changed are restored at once.
  The job page shows the edit's outcome per file and, separately, the rollback outcome (*rollback: restored*).
- **Disk full while writing the journal.** Gama then cannot record a rollback, so it does not attempt one. The job
  stops and stays open. Once there is space again, restart the worker: recovery checks every file by its content.
  You can then restore the files that were changed.

### "Recovery pending"

If a library disk is not mounted, or a file cannot be read, when recovery runs, Gama does not guess. The job is shown
as **recovery pending**, on the job page and at the top of *History*. New edits of its files are refused until
recovery has run.

- **Usually:** mount the disk and restart the worker. Recovery then finishes on its own.
- **If a library folder or root was retired or renamed for good:**
  1. Run `manage.py gama_recover --resolve JOURNAL_ID`. It re-checks every file and lists the ones that are not
     available. Nothing is changed.
  2. Run `manage.py gama_recover --resolve JOURNAL_ID --confirm-missing`. It records those files as missing and
     closes the job.
- `manage.py gama_recover` on its own runs recovery and lists anything still pending.

## 6. Backups

- Backups are kept for good. The size of the backup store is shown on the *History* page.
- A backup is shared by every job that started from the same file content.
- Gama never deletes backups. Clean them up by hand only when you are sure you will never restore those jobs.
