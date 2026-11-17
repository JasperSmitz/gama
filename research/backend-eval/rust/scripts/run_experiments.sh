#!/bin/sh
# Runs E1-E6 for the rust candidate. Outputs go to results/rust/.
# Usage (from backend-eval/): sh rust/scripts/run_experiments.sh
set -u
EVAL=${EVAL:-/tmp/claude-1000/-storage-development-gama/88084e20-e91b-48dc-a0ff-99d209baba8d/scratchpad/eval}
ROOT=$(pwd)
P=$ROOT/rust/target/release/proto
OUT=$ROOT/results/rust
G="Ambient;Electronic;Soundtrack"
VERIFY="python3 -I $ROOT/harness/verify.py"
TCON="python3 -I $ROOT/rust/scripts/id3_tcon.py"
mkdir -p "$OUT"/e1 "$OUT"/e2 "$OUT"/e3 "$OUT"/e4 "$OUT"/e5 "$OUT"/e6

prep() { sh "$ROOT/harness/prepare_work.sh" "$EVAL" rust "$1"; }
bk() { d="$EVAL/work/rust/$1.bk"; rm -rf "$d"; mkdir -p "$d"; echo "$d"; }
# sha256 of every file under a dir, relative paths
shas() { (cd "$1" && find . -type f | sort | xargs -d '\n' sha256sum); }

echo "== E1 read"
W=$(prep e1)
$P read "$W" > "$OUT/read.json"
echo "exit=$?" > "$OUT/e1/exit.txt"
python3 -I "$ROOT/rust/scripts/compare_ffprobe.py" "$OUT/read.json" > "$OUT/e1/compare_ffprobe.txt" 2>&1

echo "== E2 set-genres on albums"
W=$(prep e2); B=$(bk e2)
: > "$OUT/e2/exit.txt"
for a in real_flac real_mp3 real_m4a synth_vorbis synth_opus; do
  $P set-genres --genres "$G" --backup-dir "$B/$a" --journal "$OUT/e2/journal-$a.json" \
     "$W/albums/$a"/* > "$OUT/e2/set-genres-$a.json"
  echo "$a exit=$?" >> "$OUT/e2/exit.txt"
done
$VERIFY genres "$EVAL/master/albums" "$W/albums" --genres "$G" --json "$OUT/e2/verify_genres.json" > "$OUT/e2/verify_genres.txt" 2>&1
$P read "$W/albums" > "$OUT/e2/read_after.json"
$TCON "$W/albums/real_mp3"/* > "$OUT/e2/id3_tcon.txt"

echo "== E3 set-fields"
W=$(prep e3); B=$(bk e3)
$P set-fields --continue-on-error --backup-dir "$B" --journal "$OUT/e3/journal.json" \
  "$W/albums/real_flac/01. C418 - Key.flac" \
  "$W/albums/real_mp3/01. Joy Division - Disorder.mp3" \
  "$W/albums/real_m4a/05 - Porcupine Tree - Even Less - CLOSURE_CONTINUATION.LIVE.m4a" \
  "$W/albums/synth_vorbis/01 - Synthetic vorbis.ogg" \
  "$W/albums/synth_opus/01 - Synthetic opus.opus" \
  "$W/edge"/* > "$OUT/e3/set-fields.json"
echo "exit=$?" > "$OUT/e3/exit.txt"
$VERIFY fields "$EVAL/master/albums" "$W/albums" --json "$OUT/e3/verify_fields_albums.json" > "$OUT/e3/verify_fields_albums.txt" 2>&1
$VERIFY fields "$EVAL/master/edge" "$W/edge" --json "$OUT/e3/verify_fields_edge.json" > "$OUT/e3/verify_fields_edge.txt" 2>&1
$P read "$W" > "$OUT/e3/read_after.json"
$TCON "$W/albums/real_mp3/01. Joy Division - Disorder.mp3" "$W/edge"/*.mp3 > "$OUT/e3/id3_frames.txt" 2>&1

echo "== E4 set-genres per edge file"
W=$(prep e4); B=$(bk e4)
: > "$OUT/e4/exit.txt"
for f in "$W/edge"/*; do
  n=$(basename "$f")
  $P set-genres --continue-on-error --genres "$G" --backup-dir "$B/$n" --journal "$OUT/e4/journal-$n.json" \
     "$f" > "$OUT/e4/set-genres-$n.json"
  echo "$n exit=$?" >> "$OUT/e4/exit.txt"
done
$VERIFY genres "$EVAL/master/edge" "$W/edge" --genres "$G" --json "$OUT/e4/verify_genres.json" > "$OUT/e4/verify_genres.txt" 2>&1
$P read "$W/edge" > "$OUT/e4/read_after.json"
$TCON "$EVAL/master/edge"/mp3_*.mp3 > "$OUT/e4/id3_frames_master.txt" 2>&1
$TCON "$W/edge"/mp3_*.mp3 > "$OUT/e4/id3_frames_after.txt" 2>&1

echo "== E5a partial_failure without --continue-on-error"
W=$(prep e5a); B=$(bk e5a)
shas "$W/albums/partial_failure" > "$OUT/e5/a_sha_before.txt"
$P set-genres --genres "$G" --backup-dir "$B" --journal "$OUT/e5/a_journal.json" \
   "$W/albums/partial_failure"/* > "$OUT/e5/a_set-genres.json"
echo "exit=$?" > "$OUT/e5/a_exit.txt"
shas "$W/albums/partial_failure" > "$OUT/e5/a_sha_after.txt"
diff "$OUT/e5/a_sha_before.txt" "$OUT/e5/a_sha_after.txt" > "$OUT/e5/a_sha_diff.txt" && echo "IDENTICAL" >> "$OUT/e5/a_sha_diff.txt"
ls -la "$W/albums/partial_failure" > "$OUT/e5/a_ls_after.txt"

echo "== E5b partial_failure with --continue-on-error"
W=$(prep e5b); B=$(bk e5b)
$P set-genres --continue-on-error --genres "$G" --backup-dir "$B" --journal "$OUT/e5/b_journal.json" \
   "$W/albums/partial_failure"/* > "$OUT/e5/b_set-genres.json"
echo "exit=$?" > "$OUT/e5/b_exit.txt"
$VERIFY genres "$EVAL/master/albums/partial_failure" "$W/albums/partial_failure" --genres "$G" > "$OUT/e5/b_verify_genres.txt" 2>&1
ls -la "$W/albums/partial_failure" > "$OUT/e5/b_ls_after.txt"

echo "== E5c --fail-after 2 on real_flac"
W=$(prep e5c); B=$(bk e5c)
$P set-genres --fail-after 2 --genres "$G" --backup-dir "$B" --journal "$OUT/e5/c_journal.json" \
   "$W/albums/real_flac"/* > "$OUT/e5/c_set-genres.json"
echo "exit=$?" > "$OUT/e5/c_exit.txt"
shas "$EVAL/master/albums/real_flac" > "$OUT/e5/c_sha_master.txt"
shas "$W/albums/real_flac" > "$OUT/e5/c_sha_after.txt"
diff "$OUT/e5/c_sha_master.txt" "$OUT/e5/c_sha_after.txt" > "$OUT/e5/c_sha_diff.txt" && echo "IDENTICAL" >> "$OUT/e5/c_sha_diff.txt"
ls -la "$W/albums/real_flac" > "$OUT/e5/c_ls_after.txt"

echo "== E5d rollback after a successful batch"
W=$(prep e5d); B=$(bk e5d)
$P set-genres --genres "$G" --backup-dir "$B" --journal "$OUT/e5/d_journal.json" \
   "$W/albums/synth_vorbis"/* "$W/albums/real_mp3"/* > "$OUT/e5/d_set-genres.json"
echo "set-genres exit=$?" > "$OUT/e5/d_exit.txt"
shas "$W/albums" > "$OUT/e5/d_sha_after_write.txt"
$P rollback --journal "$OUT/e5/d_journal.json" > "$OUT/e5/d_rollback.json"
echo "rollback exit=$?" >> "$OUT/e5/d_exit.txt"
shas "$EVAL/master/albums" > "$OUT/e5/d_sha_master.txt"
shas "$W/albums" > "$OUT/e5/d_sha_after_rollback.txt"
diff "$OUT/e5/d_sha_master.txt" "$OUT/e5/d_sha_after_rollback.txt" > "$OUT/e5/d_sha_diff.txt" && echo "IDENTICAL" >> "$OUT/e5/d_sha_diff.txt"
# second rollback must be a no-op
$P rollback --journal "$OUT/e5/d_journal.json" > "$OUT/e5/d_rollback_again.json"
echo "rollback-again exit=$?" >> "$OUT/e5/d_exit.txt"

echo "== E5x what do lofty / the OS allow on the read-only file?"
W=$(prep e5x)
$P direct-save "$W/albums/partial_failure/05 readonly.m4a" > "$OUT/e5/x_direct_save_readonly.json"

echo "== W: same writes with the lofty workarounds disabled (evidence for the bugs)"
mkdir -p "$OUT/workarounds_off"
W=$(prep woff); B=$(bk woff)
for a in real_flac real_m4a; do
  PROTO_NO_MP4_INT_FIX=1 PROTO_WRITE_IMPLICIT=1 $P set-genres --continue-on-error --genres "$G" \
    --backup-dir "$B/$a" --journal "$OUT/workarounds_off/journal-$a.json" "$W/albums/$a"/* \
    > "$OUT/workarounds_off/set-genres-$a.json"
done
PROTO_KEEP_FLAC_ID3=1 $P set-genres --continue-on-error --genres "$G" --backup-dir "$B/id3flac" \
  --journal "$OUT/workarounds_off/journal-id3flac.json" "$W/edge/flac_with_id3v2_prefix.flac" \
  > "$OUT/workarounds_off/set-genres-flac_with_id3v2_prefix.json"
$VERIFY genres "$EVAL/master/albums" "$W/albums" --genres "$G" > "$OUT/workarounds_off/verify_genres_albums.txt" 2>&1

echo "== E6 resources"
W=$(prep e6)
if [ -x /usr/bin/time ]; then
  /usr/bin/time -v $P read "$W" > /dev/null 2> "$OUT/e6/time_read.txt"
else
  echo "/usr/bin/time not installed; using rust/scripts/measure.py (getrusage)" > "$OUT/e6/time_read.txt"
  for i in 1 2 3; do python3 -I "$ROOT/rust/scripts/measure.py" $P read "$W" >> "$OUT/e6/time_read.txt"; done
fi
ls -l "$P" > "$OUT/e6/binary.txt"
ldd "$P" >> "$OUT/e6/binary.txt" 2>&1
echo done
