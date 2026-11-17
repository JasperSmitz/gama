#!/bin/sh
# Make a fresh disposable copy of the master fixtures for one candidate/run.
# Usage: prepare_work.sh <eval_dir> <candidate> <run>   -> prints the work dir
set -eu
EVAL="$1"; CAND="$2"; RUN="$3"
W="$EVAL/work/$CAND/$RUN"
[ -d "$W" ] && chmod -R u+w "$W" && rm -rf "$W"
mkdir -p "$(dirname "$W")"
cp -a "$EVAL/master" "$W"
rm -f "$W/cover.jpg"
chmod 444 "$W/albums/partial_failure/05 readonly.m4a"
echo "$W"
