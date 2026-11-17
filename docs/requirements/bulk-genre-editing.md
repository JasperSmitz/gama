# Gama — Bulk Metadata Editing

## Project context

Gama is a self-hosted media collection management application written in Python.

Its purpose is to catalogue, explore, organize, and edit the metadata of media files that I personally own.

Gama is **not** a media player, recommendation service, wishlist, or general-purpose media database.

The authoritative metadata lives in the actual media files. Gama's database is an index, not a competing source of truth.

Music is the initial focus. Film support may be introduced in the future, but is explicitly outside the current scope.

## Your task

Design and implement a reliable bulk metadata editing workflow, initially focused on adding and removing album genres.

Example albums:
- In the Court of the Crimson King — King Crimson
- Selling England by the Pound — Genesis
- Close to the Edge — Yes

A user should be able to select these albums through Gama's web interface and modify their genre metadata together.

**Prioritize safety and correctness above performance.**

## Functional requirements

### 1. Album selection

Allow users to select multiple albums from their indexed collection.

The selected albums should remain visible throughout the editing process.

Album identification must be reliable, including cases involving duplicate album titles, multiple editions, and multiple discs.

### 2. Bulk genre editing

Support three distinct operations:

- **Add genre:** Add a genre to every selected track without removing existing genres.
- **Remove genre:** Remove a specified genre from every selected track while preserving other genres.
- **Replace genres:** Replace the genre set for selected tracks, but only through an explicitly separate operation.

The default editing experience should favor additive, non-destructive changes.

Support multiple genres and handle case-insensitive comparisons and duplicate values carefully.

Respect the metadata conventions of each supported audio format. Do not flatten or destroy existing genre information.

### 3. Preview before applying

No edits should be written immediately.

Show a clear preview containing:

- Every affected album and track.
- Original genre metadata.
- Proposed genre metadata.
- Number of files that will change.
- Files that require no changes.
- Warnings about unsupported or problematic files.

Require explicit user confirmation before writing anything.

If files change on disk between preview and confirmation, detect the conflict and require a refreshed preview rather than overwriting newer changes.

### 4. Safe metadata writing

Use established Python metadata libraries, preferably Mutagen where suitable.

Requirements:

- Preserve all unrelated metadata.
- Preserve embedded artwork.
- Never re-encode audio.
- Never rename, relocate, or delete source files.
- Create recoverable backups before modifying originals.
- Validate backup integrity before beginning edits.
- Check available disk space and filesystem permissions.
- Prefer editing a temporary copy, verifying its metadata, and atomically replacing the original where supported.
- Preserve relevant filesystem attributes.
- Verify metadata after each successful write.
- Handle interrupted operations and partial failures.
- Provide a recovery mechanism for restoring previous versions.

Do not claim a multi-file edit is fully atomic unless the implementation genuinely guarantees that.

### 5. Backup and recovery

Create an explicit edit history.

Each operation should record:

- Timestamp and operation identifier.
- Affected files.
- Original metadata.
- Requested modifications.
- Successful and failed writes.
- Backup locations.
- Verification results.

Allow a user to restore files from backups.

Never silently overwrite or delete backups.

### 6. User interface

Create an intuitive workflow:

**Select albums → Choose operation → Preview changes → Confirm → Apply → Review results**

The interface should clearly distinguish between adding, removing, and replacing genres.

Show progress and individual failures when applying edits.

After completion, update Gama's collection index to reflect the actual metadata read back from disk.

### 7. Testing

Use temporary synthetic music libraries, never my production collection.

Test at least:

- Single and multi-disc albums.
- Multiple genres on one track.
- Adding an existing genre.
- Removing a nonexistent genre.
- Preserving unrelated tags and artwork.
- Albums with inconsistent track metadata.
- Unsupported and corrupted files.
- Missing permissions.
- Insufficient storage for backups.
- A file changing between preview and application.
- Interrupted or partially failed operations.
- Successful restoration from backups.
- Repeated operations and their idempotence.

Use real test audio fixtures where appropriate rather than relying exclusively on mocked metadata.

## Architecture

Use Python and established, maintained libraries.

Separate these responsibilities:

1. Collection indexing and querying.
2. Metadata reading.
3. Change planning and preview generation.
4. Safe metadata writing.
5. Backup and recovery.
6. Edit history and verification.
7. Web API and user interface.

Keep the editing logic independent of the web framework so that it can be tested directly.

Favor simple, maintainable solutions over elaborate abstractions.

Do not introduce film-specific metadata handling yet.

## Development workflow

Use the Architect, Developer, and Reviewer agents.

The Architect should first inspect the existing repository and design a milestone-based implementation consistent with Gama's current architecture.

The Developer should implement the milestones and accompanying tests.

The Reviewer should critically assess correctness, data preservation, recoverability, and edge cases.

Do not treat successful writes as proof of correctness. Validate that the intended metadata changed and that unrelated metadata survived.

Continue autonomously through independently testable milestones.

## Safety constraints

Do not access or modify my real `/storage/music` library during development.

Do not modify Navidrome, Coda, Liner, Gargoyle, or existing synchronization services.

Do not expose metadata-writing functionality publicly without appropriate authentication and authorization.

Do not add automatic metadata matching, external catalogue lookups, or unrelated features.

## Definition of done

A user can select several albums, safely add or remove genres across their tracks, inspect every proposed modification, confirm the changes, and recover from mistakes.

The implementation must have meaningful automated tests and clear documentation.

**A reliable, limited implementation is preferable to a feature-rich application that risks corrupting the collection.**
