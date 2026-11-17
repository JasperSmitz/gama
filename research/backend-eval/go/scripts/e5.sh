#!/bin/bash
# E5 partial failure + read-only + SIGKILL experiments. Run from backend-eval/.
# usage: go/scripts/e5.sh <EVAL> > results/go/e5.txt
set -u
EVAL=$1; S=$EVAL/side/go-e5; rm -rf "$S"; mkdir -p "$S"; G="Ambient;Electronic;Soundtrack"
shas(){ (cd "$1" && sha256sum -- *); }
summ(){ python3 -c "
import json,sys;t=open(sys.argv[1]).read();d=json.loads(t[t.index('{'):t.rindex('}')+1])
print(json.dumps({k:d.get(k) for k in ('ok','aborted','committed','failed','skipped','rolled_back','leftover_temp_files') if k in d}, ensure_ascii=False))
for e in d.get('entries',d.get('files',[])): print('   ', e.get('state',e.get('action')), e['path'].split('/')[-1], (e.get('error') or '')[:160])" "$1"; }
run(){ echo "\$ proto $*" | sed "s|$EVAL|\$EVAL|g"; go/bin/proto "$@" > "$S/last.out" 2>&1; echo "    exit $?"; summ "$S/last.out"; }
clean(){ chmod -R u+w "$1"; rm -rf "$1"; }

W=$(harness/prepare_work.sh "$EVAL" go e5a); P=$W/albums/partial_failure
echo "## (a) partial_failure without --continue-on-error"; shas "$P" > "$S/a.before"
run set-genres --genres "$G" --backup-dir "$S/a.bak" --journal "$S/a.json" "$P"/*
shas "$P" | diff "$S/a.before" - && echo "sha256: all 5 files identical before/after (zero modified)"; clean "$W"

W=$(harness/prepare_work.sh "$EVAL" go e5b); P=$W/albums/partial_failure
echo; echo "## (b) partial_failure with --continue-on-error"; shas "$P" > "$S/b.before"
run set-genres --genres "$G" --continue-on-error --backup-dir "$S/b.bak" --journal "$S/b.json" "$P"/*
echo "files whose sha256 changed:"; shas "$P" | diff "$S/b.before" - | grep '^>'; ls -l "$P" | sed "s|$EVAL|\$EVAL|g"
python3 -I harness/verify.py genres "$EVAL/master/albums/partial_failure" "$P" --genres "$G"; clean "$W"

W=$(harness/prepare_work.sh "$EVAL" go e5c); P=$W/albums/real_flac
echo; echo "## (c) --fail-after 2 on albums/real_flac"; shas "$P" > "$S/c.before"
run set-genres --genres "$G" --fail-after 2 --backup-dir "$S/c.bak" --journal "$S/c.json" "$P"/*
shas "$P" | diff "$S/c.before" - && echo "sha256: all 3 files identical to pre-batch (full rollback)"; echo "dir listing:"; ls -A "$P"; clean "$W"

W=$(harness/prepare_work.sh "$EVAL" go e5d); P=$W/albums/synth_vorbis
echo; echo "## (d) successful batch on albums/synth_vorbis, then rollback --journal"; shas "$P" > "$S/d.before"
run set-genres --genres "$G" --backup-dir "$S/d.bak" --journal "$S/d.json" "$P"/*
echo "files changed by batch: $(shas "$P" | diff "$S/d.before" - | grep -c '^>')"
run rollback --journal "$S/d.json"
shas "$P" | diff "$S/d.before" - && echo "sha256: all 3 files identical to pre-batch after rollback"; ls -A "$P"; clean "$W"

echo; echo "## (e) read-only file: what would the library / OS allow?"
cp "$EVAL/master/albums/partial_failure/05 readonly.m4a" "$S/ro.m4a"; chmod 444 "$S/ro.m4a"; s0=$(sha256sum < "$S/ro.m4a")
echo "direct in-place taglib.WriteTags on 0444 file:"; go/bin/proto raw-write "$S/ro.m4a" "GENRE=X" | tr -d '\n'; echo
[ "$s0" = "$(sha256sum < "$S/ro.m4a")" ] && echo "-> file unchanged: TagLib opens read-only and save() fails (ErrSavingFile, no detail)"
cp "$S/ro.m4a" "$S/other"; mv -f "$S/other" "$S/ro.m4a" && echo "-> but rename(2) over the 0444 file succeeds (needs only dir write perm): without the preflight W_OK check, the temp+rename protocol WOULD modify it"

echo; echo "## (f) SIGKILL mid-batch (copy of albums/real_m4a), then rollback --journal"
P=$EVAL/work/go/kill/real_m4a
for d in 0.03 0.05 0.08 0.12; do
  rm -rf "$EVAL/work/go/kill" "$S/k.bak" "$S"/k.json*; mkdir -p "$P"; cp -a "$EVAL/master/albums/real_m4a/." "$P/"; shas "$P" > "$S/k.before"
  go/bin/proto set-genres --genres "A;B" --backup-dir "$S/k.bak" --journal "$S/k.json" "$P"/* > /dev/null 2>&1 & pid=$!
  python3 -c "import time;time.sleep($d)"; kill -9 $pid 2>/dev/null; wait $pid 2>/dev/null
  st=$(python3 -c "import json;print(' '.join(e['state'] for e in json.load(open('$S/k.json'))['entries']))" 2>/dev/null)
  if echo "$st" | grep -q writing; then
    echo "killed after ${d}s; journal states: $st"; echo "dir after kill:"; ls -A "$P"
    run rollback --journal "$S/k.json"; echo "dir after rollback:"; ls -A "$P"
    shas "$P" | diff "$S/k.before" - && echo "sha256 identical to pre-batch after rollback"; break
  fi
done
rm -rf "$EVAL/work/go/kill" "$S"
