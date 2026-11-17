#!/bin/bash
# Re-run E2, E3, E4 into fresh work dirs (kept). Run from backend-eval/.
# usage: go/scripts/e234.sh <EVAL>   -> writes results/go/e{2,3,4}_*.txt|json
set -u
EVAL=$1; R=results/go; G="Ambient;Electronic;Soundtrack"; S=$EVAL/side/go-e234
mkdir -p "$S"
strip(){ sed "s|$EVAL|\$EVAL|g"; }

# E2: one batch over the 5 albums, then verify each album separately
W=$(harness/prepare_work.sh "$EVAL" go e2); A=$W/albums
{ echo "\$ proto set-genres --genres '$G' --backup-dir \$S/e2.bak --journal \$S/e2.json \$W/albums/{real_flac,real_mp3,real_m4a,synth_vorbis,synth_opus}/*"
  go/bin/proto set-genres --genres "$G" --backup-dir "$S/e2.bak" --journal "$S/e2.json" "$A"/{real_flac,real_mp3,real_m4a,synth_vorbis,synth_opus}/*; echo "exit $?"; } 2>&1 | strip > $R/e2_set_genres.txt
: > $R/e2_verify.txt
for al in real_flac real_mp3 real_m4a synth_vorbis synth_opus; do
  echo "\$ verify.py genres \$EVAL/master/albums/$al \$W/albums/$al --genres '$G' --require-changed" >> $R/e2_verify.txt
  python3 -I harness/verify.py genres "$EVAL/master/albums/$al" "$A/$al" --genres "$G" --require-changed --json "$R/e2_verify_$al.json" 2>&1 | strip >> $R/e2_verify.txt
  echo "exit ${PIPESTATUS[0]}" >> $R/e2_verify.txt; echo >> $R/e2_verify.txt
done
go/bin/proto read "$A" > $R/e2_reread.json

# E3: set-fields on one file per album format + all edge files (preflight skips unreadable)
W=$(harness/prepare_work.sh "$EVAL" go e3); A=$W/albums
{ echo "\$ proto set-fields --continue-on-error --backup-dir \$S/e3.bak --journal \$S/e3.json <1 file per album format> \$W/edge/*"
  go/bin/proto set-fields --continue-on-error --backup-dir "$S/e3.bak" --journal "$S/e3.json" \
    "$A/real_flac/01. C418 - Key.flac" "$A/real_mp3/01. Joy Division - Disorder.mp3" \
    "$A/real_m4a/05 - Porcupine Tree - Even Less - CLOSURE_CONTINUATION.LIVE.m4a" \
    "$A/synth_vorbis/01 - Synthetic vorbis.ogg" "$A/synth_opus/01 - Synthetic opus.opus" "$W"/edge/*; echo "exit $?"; } 2>&1 | strip > $R/e3_set_fields.txt
{ echo "\$ verify.py fields \$EVAL/master \$W"; python3 -I harness/verify.py fields "$EVAL/master" "$W" --json $R/e3_verify.json; echo "exit $?"; } 2>&1 | strip > $R/e3_verify.txt

# E4: set-genres on each edge file separately
W=$(harness/prepare_work.sh "$EVAL" go e4); : > $R/e4_set_genres.txt
for f in "$W"/edge/*; do b=$(basename "$f")
  { echo "\$ proto set-genres --genres '$G' --continue-on-error --backup-dir \$S/e4-$b.bak --journal \$S/e4-$b.json \$W/edge/$b"
    go/bin/proto set-genres --genres "$G" --continue-on-error --backup-dir "$S/e4-$b.bak" --journal "$S/e4-$b.json" "$f"; echo "exit $?"; } 2>&1 | strip >> $R/e4_set_genres.txt
done
{ echo "\$ verify.py genres \$EVAL/master/edge \$W/edge --genres '$G'"; python3 -I harness/verify.py genres "$EVAL/master/edge" "$W/edge" --genres "$G" --json $R/e4_verify.json; echo "exit $?"; } 2>&1 | strip > $R/e4_verify.txt
go/bin/proto read "$W/edge" > $R/e4_reread.json
