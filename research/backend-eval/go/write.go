package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"go.senan.xyz/taglib"
	"golang.org/x/sys/unix"
)

// ---- journal ---------------------------------------------------------------

type Entry struct {
	Path         string `json:"path"`
	Backup       string `json:"backup,omitempty"`
	Temp         string `json:"temp,omitempty"`
	SHA256Before string `json:"sha256_before,omitempty"`
	SHA256After  string `json:"sha256_after,omitempty"`
	Mode         uint32 `json:"mode,omitempty"`
	// State: pending | preflight_failed | backed_up | writing | committed | failed | rolled_back | rollback_failed
	State string `json:"state"`
	Error string `json:"error,omitempty"`
}

type Journal struct {
	Op      string              `json:"op"`
	Values  map[string][]string `json:"values,omitempty"`
	Created string              `json:"created"`
	Entries []*Entry            `json:"entries"`
	path    string
}

// save writes the journal atomically and durably: tmp + fsync + rename + dir fsync.
func (j *Journal) save() error {
	b, err := json.MarshalIndent(j, "", "  ")
	if err != nil {
		return err
	}
	tmp := j.path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(b); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, j.path); err != nil {
		return err
	}
	return syncDir(filepath.Dir(j.path))
}

func loadJournal(p string) (*Journal, error) {
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	j := &Journal{}
	if err := json.Unmarshal(b, j); err != nil {
		return nil, err
	}
	j.path = p
	return j, nil
}

// ---- fs helpers --------------------------------------------------------------

func syncDir(d string) error {
	f, err := os.Open(d)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()
	return f.Sync()
}

// copyDurable copies src to the already-open dst, fsyncs and closes dst.
func copyDurable(src string, dst *os.File) error {
	in, err := os.Open(src)
	if err != nil {
		_ = dst.Close()
		return err
	}
	defer func() { _ = in.Close() }()
	if _, err := io.Copy(dst, in); err != nil {
		_ = dst.Close()
		return err
	}
	if err := dst.Sync(); err != nil {
		_ = dst.Close()
		return err
	}
	return dst.Close()
}

// tempBeside creates a hidden temp file next to p. The original extension is kept
// as the suffix because TagLib's FileRef chooses its parser by extension first.
func tempBeside(p string) (*os.File, error) {
	dir, base := filepath.Split(p)
	ext := filepath.Ext(base)
	return os.CreateTemp(dir, "."+strings.TrimSuffix(base, ext)+".gamatmp-*"+ext)
}

// replaceFrom copies src into a temp file beside dst, sets mode, renames it over dst
// and fsyncs the directory. Used for both rollback and restore.
func replaceFrom(src, dst string, mode os.FileMode) error {
	t, err := tempBeside(dst)
	if err != nil {
		return err
	}
	tmp := t.Name()
	if err := copyDurable(src, t); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, dst); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return syncDir(filepath.Dir(dst))
}

// ---- verification ------------------------------------------------------------

func applyChanges(m map[string][]string, ch map[string][]string) map[string][]string {
	out := make(map[string][]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	for k, v := range ch {
		if len(v) == 0 {
			delete(out, k)
		} else {
			out[k] = v
		}
	}
	return out
}

func diffTags(want, got map[string][]string) []string {
	keys := map[string]bool{}
	for k := range want {
		keys[k] = true
	}
	for k := range got {
		keys[k] = true
	}
	var ks []string
	for k := range keys {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	var d []string
	for _, k := range ks {
		if !reflect.DeepEqual(want[k], got[k]) {
			d = append(d, fmt.Sprintf("%s: want %q got %q", k, want[k], got[k]))
		}
	}
	return d
}

func verifyAfter(before, after ReadResult, ch map[string][]string) error {
	if !after.OK {
		return fmt.Errorf("re-read failed: %s", after.Error)
	}
	var probs []string
	if before.Format != after.Format {
		probs = append(probs, fmt.Sprintf("format %s -> %s", before.Format, after.Format))
	}
	if before.LengthMs != after.LengthMs || before.SampleRate != after.SampleRate || before.Channels != after.Channels {
		probs = append(probs, "audio properties changed")
	}
	probs = append(probs, diffTags(applyChanges(before.raw, ch), after.raw)...)
	if !reflect.DeepEqual(before.Pictures, after.Pictures) {
		probs = append(probs, fmt.Sprintf("pictures changed: %d -> %d", len(before.Pictures), len(after.Pictures)))
	}
	if len(probs) > 0 {
		return fmt.Errorf("verify: %s", strings.Join(probs, "; "))
	}
	return nil
}

// ---- preflight ----------------------------------------------------------------

func preflight(p string) (ReadResult, os.FileMode, error) {
	st, err := os.Lstat(p)
	if err != nil {
		return ReadResult{}, 0, err
	}
	if !st.Mode().IsRegular() {
		return ReadResult{}, 0, errors.New("not a regular file")
	}
	if err := unix.Access(p, unix.W_OK); err != nil {
		// Note: our rename-based write would succeed anyway (rename needs only
		// directory write permission), so this check is the only thing stopping it.
		return ReadResult{}, 0, fmt.Errorf("no write permission on file: %w", err)
	}
	if err := unix.Access(filepath.Dir(p), unix.W_OK|unix.X_OK); err != nil {
		return ReadResult{}, 0, fmt.Errorf("directory not writable (needed for temp+rename): %w", err)
	}
	r := readFile(p)
	if !r.OK {
		return r, 0, fmt.Errorf("library read failed: %s", r.Error)
	}
	return r, st.Mode().Perm(), nil
}

// ---- batch --------------------------------------------------------------------

type BatchOpts struct {
	Op              string
	Values          map[string][]string
	Changes         func(ReadResult) map[string][]string
	BackupDir       string
	JournalPath     string
	ContinueOnError bool
	FailAfter       int // <0: disabled
}

type BatchReport struct {
	OK         bool     `json:"ok"`
	Op         string   `json:"op"`
	Journal    string   `json:"journal"`
	Aborted    string   `json:"aborted,omitempty"`
	Committed  int      `json:"committed"`
	Failed     int      `json:"failed"`
	Skipped    int      `json:"skipped"`
	RolledBack int      `json:"rolled_back"`
	Entries    []*Entry `json:"entries"`
	LeftTemps  []string `json:"leftover_temp_files"`
	ElapsedMs  int64    `json:"elapsed_ms"`
}

func runBatch(o BatchOpts, files []string) int {
	start := time.Now()
	j := &Journal{Op: o.Op, Values: o.Values, Created: time.Now().Format(time.RFC3339), path: o.JournalPath}
	rep := &BatchReport{Op: o.Op, Journal: o.JournalPath}
	finish := func(code int) int {
		rep.Entries = j.Entries
		rep.LeftTemps = leftoverTemps(files)
		rep.ElapsedMs = time.Since(start).Milliseconds()
		rep.OK = code == 0
		return emit(rep, code)
	}
	if err := os.MkdirAll(o.BackupDir, 0o755); err != nil {
		rep.Aborted = "backup dir: " + err.Error()
		return finish(2)
	}

	// 1. preflight everything before touching anything
	type item struct {
		e      *Entry
		before ReadResult
	}
	var items []item
	pfFail := 0
	for _, f := range files {
		abs, err := filepath.Abs(f)
		if err != nil {
			abs = f
		}
		e := &Entry{Path: abs}
		j.Entries = append(j.Entries, e)
		r, mode, err := preflight(abs)
		if err != nil {
			e.State, e.Error = "preflight_failed", err.Error()
			pfFail++
			continue
		}
		e.Mode, e.State = uint32(mode), "pending"
		items = append(items, item{e, r})
	}
	rep.Skipped = pfFail
	if err := j.save(); err != nil {
		rep.Aborted = "journal: " + err.Error()
		return finish(2)
	}
	if pfFail > 0 && !o.ContinueOnError {
		rep.Aborted = fmt.Sprintf("preflight failed for %d file(s); nothing modified", pfFail)
		return finish(1)
	}

	rollbackCommitted := func() {
		for _, e := range j.Entries {
			if e.State != "committed" {
				continue
			}
			if err := restoreEntry(e); err != nil {
				e.State, e.Error = "rollback_failed", err.Error()
			} else {
				e.State = "rolled_back"
				rep.RolledBack++
			}
			_ = j.save()
		}
		rep.Committed = 0
	}

	for i, it := range items {
		if o.FailAfter >= 0 && rep.Committed >= o.FailAfter {
			rep.Aborted = fmt.Sprintf("simulated failure after %d commit(s) (--fail-after)", o.FailAfter)
			rollbackCommitted()
			return finish(1)
		}
		err := writeOne(j, it.e, it.before, o, i)
		if err == nil {
			rep.Committed++
			continue
		}
		it.e.State, it.e.Error = "failed", err.Error()
		_ = j.save()
		rep.Failed++
		if !o.ContinueOnError {
			rep.Aborted = fmt.Sprintf("%s: %v; rolling back batch", it.e.Path, err)
			rollbackCommitted()
			return finish(1)
		}
	}
	if o.FailAfter >= 0 && rep.Committed >= o.FailAfter && len(items) > 0 {
		rep.Aborted = fmt.Sprintf("simulated failure after %d commit(s) (--fail-after)", o.FailAfter)
		rollbackCommitted()
		return finish(1)
	}
	if rep.Failed > 0 || rep.Skipped > 0 {
		return finish(1)
	}
	return finish(0)
}

// writeOne runs steps 2-5 of the protocol for a single file.
func writeOne(j *Journal, e *Entry, before ReadResult, o BatchOpts, idx int) (err error) {
	// 2. backup + journal
	e.Backup = filepath.Join(o.BackupDir, fmt.Sprintf("%04d-%s", idx, filepath.Base(e.Path)))
	bf, err := os.OpenFile(e.Backup, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return fmt.Errorf("backup: %w", err)
	}
	if err := copyDurable(e.Path, bf); err != nil {
		return fmt.Errorf("backup: %w", err)
	}
	if err := syncDir(o.BackupDir); err != nil {
		return err
	}
	if e.SHA256Before, err = fileSHA(e.Path); err != nil {
		return err
	}
	if bs, err := fileSHA(e.Backup); err != nil || bs != e.SHA256Before {
		return fmt.Errorf("backup sha mismatch or unreadable (file changed during copy?)")
	}
	e.State = "backed_up"
	if err := j.save(); err != nil {
		return fmt.Errorf("journal: %w", err)
	}

	// 3. temp copy in same dir, library write, fsync
	t, err := tempBeside(e.Path)
	if err != nil {
		return fmt.Errorf("temp: %w", err)
	}
	tmp := t.Name()
	e.Temp, e.State = tmp, "writing"
	committed := false
	defer func() {
		if !committed {
			_ = os.Remove(tmp) // 8. never leave temp files behind
		}
	}()
	if err := j.save(); err != nil {
		return fmt.Errorf("journal: %w", err)
	}
	if err := copyDurable(e.Path, t); err != nil {
		return fmt.Errorf("temp copy: %w", err)
	}
	ch := o.Changes(before)
	if _, err := guarded("WriteTags", func() (struct{}, error) { return struct{}{}, taglib.WriteTags(tmp, ch, 0) }); err != nil {
		return fmt.Errorf("library write: %w", err)
	}
	if err := fsyncPath(tmp); err != nil {
		return err
	}

	// 4. reopen and verify
	after := readFile(tmp)
	if os.Getenv("PROTO_FAULT_VERIFY") == filepath.Base(e.Path) { // test hook
		after.raw = applyChanges(after.raw, map[string][]string{"INJECTED_FAULT": {"x"}})
	}
	if err := verifyAfter(before, after, ch); err != nil {
		return err
	}

	// 5. atomic rename, preserve mode, journal commit
	if cur, err := fileSHA(e.Path); err != nil || cur != e.SHA256Before {
		return fmt.Errorf("original changed underneath us; refusing to overwrite")
	}
	if err := os.Chmod(tmp, os.FileMode(e.Mode)); err != nil {
		return err
	}
	if err := os.Rename(tmp, e.Path); err != nil {
		return fmt.Errorf("rename: %w", err)
	}
	committed = true
	if err := syncDir(filepath.Dir(e.Path)); err != nil {
		return err
	}
	e.SHA256After, _ = fileSHA(e.Path)
	e.Temp, e.State = "", "committed"
	return j.save()
}

func fsyncPath(p string) error {
	f, err := os.Open(p)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()
	return f.Sync()
}

func restoreEntry(e *Entry) error {
	if e.Backup == "" {
		return errors.New("no backup recorded")
	}
	if cur, err := fileSHA(e.Path); err == nil && cur == e.SHA256Before {
		return nil // already original
	}
	if bs, err := fileSHA(e.Backup); err != nil || bs != e.SHA256Before {
		return fmt.Errorf("backup missing or corrupt: %v", err)
	}
	if err := replaceFrom(e.Backup, e.Path, os.FileMode(e.Mode)); err != nil {
		return err
	}
	if cur, err := fileSHA(e.Path); err != nil || cur != e.SHA256Before {
		return fmt.Errorf("restored file sha mismatch")
	}
	return nil
}

func leftoverTemps(files []string) []string {
	dirs := map[string]bool{}
	for _, f := range files {
		if abs, err := filepath.Abs(f); err == nil {
			dirs[filepath.Dir(abs)] = true
		}
	}
	out := []string{}
	for d := range dirs {
		m, _ := filepath.Glob(filepath.Join(d, ".*.gamatmp-*"))
		out = append(out, m...)
	}
	sort.Strings(out)
	return out
}

func cmdRollback(jpath string) int {
	j, err := loadJournal(jpath)
	if err != nil {
		return emit(map[string]any{"ok": false, "error": err.Error()}, 2)
	}
	type res struct {
		Path     string `json:"path"`
		Action   string `json:"action"`
		Verified bool   `json:"sha256_verified"`
		Error    string `json:"error,omitempty"`
	}
	var out []res
	ok := true
	var files []string
	for _, e := range j.Entries {
		files = append(files, e.Path)
		if e.Temp != "" {
			_ = os.Remove(e.Temp) // crash leftovers
		}
		if e.Backup == "" {
			out = append(out, res{e.Path, "skipped (no backup: " + e.State + ")", false, ""})
			continue
		}
		cur, _ := fileSHA(e.Path)
		action := "restored"
		if cur == e.SHA256Before {
			action = "already original"
		}
		if err := restoreEntry(e); err != nil {
			ok = false
			e.State, e.Error = "rollback_failed", err.Error()
			out = append(out, res{e.Path, "failed", false, err.Error()})
			continue
		}
		e.State = "rolled_back"
		out = append(out, res{e.Path, action, true, ""})
	}
	_ = j.save()
	code := 0
	if !ok {
		code = 1
	}
	return emit(map[string]any{"ok": ok, "journal": jpath, "files": out, "leftover_temp_files": leftoverTemps(files)}, code)
}
