// Command proto is the Go candidate of the Gama backend evaluation (disposable).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"go.senan.xyz/taglib"
)

func emit(v any, code int) int {
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	return code
}

func usage() int {
	fmt.Fprintln(os.Stderr, `usage:
  proto read <paths...>
  proto set-genres --genres "A;B" --backup-dir D --journal J.json [--continue-on-error] [--fail-after N] <files...>
  proto set-fields [--backup-dir D --journal J.json] [--continue-on-error] <files...>
  proto rollback --journal J.json
  proto raw-write <file> KEY=V1;V2 ...   (diagnostic: in-place library write, no protocol)`)
	return 2
}

func splitList(s string) []string {
	var out []string
	for _, g := range strings.Split(s, ";") {
		if g = strings.TrimSpace(g); g != "" {
			out = append(out, g)
		}
	}
	return out
}

// fieldChanges picks the native spelling of track/disc totals per format.
// Vorbis comments get separate TRACKTOTAL/DISCTOTAL fields; ID3v2 TRCK/TPOS and
// MP4 trkn/disk carry "n/m" in TagLib's property map.
func fieldChanges(r ReadResult) map[string][]string {
	ch := map[string][]string{
		"TITLE": {"Gama Title ✓"}, "ARTIST": {"Gama Artist"}, "ALBUM": {"Gama Album"},
		"ALBUMARTIST": {"Gama Album Artist"}, "DATE": {"2024"},
	}
	if strings.HasPrefix(r.Format, "flac") || strings.HasPrefix(r.Format, "ogg") {
		ch["TRACKNUMBER"], ch["TRACKTOTAL"] = []string{"7"}, []string{"12"}
		ch["DISCNUMBER"], ch["DISCTOTAL"] = []string{"2"}, []string{"3"}
	} else {
		ch["TRACKNUMBER"], ch["DISCNUMBER"] = []string{"7/12"}, []string{"2/3"}
	}
	return ch
}

func run() int {
	if len(os.Args) < 2 {
		return usage()
	}
	cmd, args := os.Args[1], os.Args[2:]
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	genres := fs.String("genres", "", "")
	backup := fs.String("backup-dir", "", "")
	journal := fs.String("journal", "", "")
	coe := fs.Bool("continue-on-error", false, "")
	failAfter := fs.Int("fail-after", -1, "")

	switch cmd {
	case "read":
		if len(args) == 0 {
			return usage()
		}
		return cmdRead(args)
	case "set-genres", "set-fields":
		if err := fs.Parse(args); err != nil {
			return usage()
		}
		o := BatchOpts{Op: cmd, BackupDir: *backup, JournalPath: *journal, ContinueOnError: *coe, FailAfter: *failAfter}
		if cmd == "set-genres" {
			gs := splitList(*genres)
			if len(gs) == 0 || o.BackupDir == "" || o.JournalPath == "" {
				return usage()
			}
			o.Values = map[string][]string{"GENRE": gs}
			o.Changes = func(ReadResult) map[string][]string { return o.Values }
		} else {
			if o.BackupDir == "" || o.JournalPath == "" {
				d, err := os.MkdirTemp("", "proto-set-fields-")
				if err != nil {
					return emit(map[string]any{"ok": false, "error": err.Error()}, 2)
				}
				if o.BackupDir == "" {
					o.BackupDir = filepath.Join(d, "backup")
				}
				if o.JournalPath == "" {
					o.JournalPath = filepath.Join(d, "journal.json")
				}
			}
			o.Changes = fieldChanges
		}
		if fs.NArg() == 0 {
			return usage()
		}
		return runBatch(o, fs.Args())
	case "rollback":
		if err := fs.Parse(args); err != nil || *journal == "" {
			return usage()
		}
		return cmdRollback(*journal)
	case "raw-write":
		if len(args) < 2 {
			return usage()
		}
		tags := map[string][]string{}
		for _, kv := range args[1:] {
			k, v, _ := strings.Cut(kv, "=")
			tags[k] = splitList(v)
		}
		err := taglib.WriteTags(args[0], tags, 0)
		res := map[string]any{"ok": err == nil, "path": args[0]}
		if err != nil {
			res["error"] = err.Error()
		}
		return emit(res, 0)
	}
	return usage()
}

func main() { os.Exit(run()) }
