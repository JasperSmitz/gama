package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"go.senan.xyz/taglib"
)

// libTimeout bounds every library call. go-taglib calls wazero with
// context.Background(), so a hung call cannot be cancelled: we can only stop
// waiting for it (the goroutine leaks). Good enough to keep the CLI from hanging.
const libTimeout = 30 * time.Second

// guarded runs fn in a goroutine, converting panics and timeouts to errors.
func guarded[T any](what string, fn func() (T, error)) (T, error) {
	type res struct {
		v   T
		err error
	}
	ch := make(chan res, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				var zero T
				ch <- res{zero, fmt.Errorf("%s: library panic: %v", what, r)}
			}
		}()
		v, err := fn()
		ch <- res{v, err}
	}()
	select {
	case r := <-ch:
		return r.v, r.err
	case <-time.After(libTimeout):
		var zero T
		return zero, fmt.Errorf("%s: timed out after %s (call leaked)", what, libTimeout)
	}
}

type Picture struct {
	Type        string `json:"type"`
	Description string `json:"description"`
	Mime        string `json:"mime"`
	Bytes       int    `json:"bytes"`
	SHA256      string `json:"sha256"`
}

type Detection struct {
	ByExtension string `json:"by_extension"` // what the extension claims
	ByContent   string `json:"by_content"`   // our own magic-byte sniff
	Library     string `json:"library"`      // TagLib's resolved format/codec
	// Method is how TagLib chose its parser: FileRef tries the extension first and
	// only falls back to content sniffing when that parser rejects the file.
	Method string `json:"method"`
}

type ReadResult struct {
	Path        string              `json:"path"`
	OK          bool                `json:"ok"`
	Error       string              `json:"error,omitempty"`
	Format      string              `json:"format"`
	Detection   Detection           `json:"detection"`
	Title       string              `json:"title"`
	Artist      string              `json:"artist"`
	Album       string              `json:"album"`
	AlbumArtist string              `json:"album_artist"`
	Year        *int                `json:"year"`
	Track       *int                `json:"track"`
	TrackTotal  *int                `json:"track_total"`
	Disc        *int                `json:"disc"`
	DiscTotal   *int                `json:"disc_total"`
	Genres      []string            `json:"genres"`
	Pictures    []Picture           `json:"pictures"`
	OtherTags   map[string][]string `json:"other_tags"`
	LengthMs    int64               `json:"length_ms"`
	SampleRate  uint                `json:"sample_rate"`
	Channels    uint                `json:"channels"`
	Warnings    []string            `json:"warnings,omitempty"`
	ElapsedMs   float64             `json:"elapsed_ms"`

	raw map[string][]string // full property map, for write verification
}

var coreKeys = map[string]bool{
	"TITLE": true, "ARTIST": true, "ALBUM": true, "ALBUMARTIST": true, "DATE": true,
	"TRACKNUMBER": true, "TRACKTOTAL": true, "DISCNUMBER": true, "DISCTOTAL": true, "GENRE": true,
}

func extFormat(p string) string {
	switch strings.ToLower(filepath.Ext(p)) {
	case ".flac":
		return "flac"
	case ".mp3":
		return "mpeg"
	case ".m4a", ".mp4", ".m4b", ".alac", ".aac":
		return "mp4"
	case ".ogg", ".oga", ".opus":
		return "ogg"
	}
	return "unknown"
}

// sniff identifies the container from magic bytes, independently of TagLib.
func sniff(p string) string {
	f, err := os.Open(p)
	if err != nil {
		return "unreadable"
	}
	defer func() { _ = f.Close() }()
	buf := make([]byte, 64)
	n, _ := io.ReadFull(f, buf)
	buf = buf[:n]
	if n == 0 {
		return "empty"
	}
	off := int64(0)
	if bytes.HasPrefix(buf, []byte("ID3")) && n >= 10 {
		sz := int64(buf[6]&0x7f)<<21 | int64(buf[7]&0x7f)<<14 | int64(buf[8]&0x7f)<<7 | int64(buf[9]&0x7f)
		off = 10 + sz
		if buf[5]&0x10 != 0 {
			off += 10
		}
		b2 := make([]byte, 4)
		if _, err := f.ReadAt(b2, off); err == nil {
			if string(b2) == "fLaC" {
				return "flac (behind ID3v2)"
			}
			if b2[0] == 0xff && b2[1]&0xe0 == 0xe0 {
				return "mpeg"
			}
		}
		return "id3v2+unknown"
	}
	switch {
	case bytes.HasPrefix(buf, []byte("fLaC")):
		return "flac"
	case bytes.HasPrefix(buf, []byte("OggS")):
		if bytes.Contains(buf, []byte("OpusHead")) {
			return "ogg/opus"
		}
		if bytes.Contains(buf, []byte("\x01vorbis")) {
			return "ogg/vorbis"
		}
		return "ogg"
	case n >= 8 && string(buf[4:8]) == "ftyp":
		return "mp4"
	case n >= 2 && buf[0] == 0xff && buf[1]&0xe0 == 0xe0:
		return "mpeg"
	}
	return "unknown"
}

var leadInt = regexp.MustCompile(`^\s*(\d+)`)
var slashTot = regexp.MustCompile(`^\s*\d+\s*/\s*(\d+)`)

func atoiPtr(s string, re *regexp.Regexp) *int {
	m := re.FindStringSubmatch(s)
	if m == nil {
		return nil
	}
	v, err := strconv.Atoi(m[1])
	if err != nil {
		return nil
	}
	return &v
}

func first(m map[string][]string, k string) string {
	if v := m[k]; len(v) > 0 {
		return v[0]
	}
	return ""
}

func sha(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func fileSHA(p string) (string, error) {
	f, err := os.Open(p)
	if err != nil {
		return "", err
	}
	defer func() { _ = f.Close() }()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// readFile does a full library read: property map, audio properties, and every picture.
func readFile(p string) (r ReadResult) {
	start := time.Now()
	r = ReadResult{Path: p, Genres: []string{}, Pictures: []Picture{}, OtherTags: map[string][]string{}}
	r.Detection.ByExtension = extFormat(p)
	r.Detection.ByContent = sniff(p)
	defer func() { r.ElapsedMs = float64(time.Since(start).Microseconds()) / 1000 }()
	fail := func(err error) ReadResult {
		r.OK = false
		r.Error = err.Error()
		r.ElapsedMs = float64(time.Since(start).Microseconds()) / 1000
		return r
	}

	st, err := os.Stat(p)
	if err != nil {
		return fail(err)
	}
	if !st.Mode().IsRegular() {
		return fail(errors.New("not a regular file"))
	}

	props, err := guarded("ReadProperties", func() (taglib.Properties, error) { return taglib.ReadProperties(p) })
	if err != nil {
		return fail(err)
	}
	if props.Format == "" {
		// taglib_file_read_properties returns NULL for isNull() files; the Go side
		// then silently returns a zero Properties and nil error.
		return fail(errors.New("library could not open file (ReadProperties returned empty Properties, no error)"))
	}
	tags, err := guarded("ReadTags", func() (map[string][]string, error) { return taglib.ReadTags(p) })
	if err != nil {
		return fail(err)
	}
	r.raw = tags
	r.Format = props.Format
	if props.InnerCodec != "" {
		r.Format += "/" + props.InnerCodec
	}
	r.Detection.Library = r.Format
	if props.Format == r.Detection.ByExtension {
		r.Detection.Method = "extension"
	} else {
		r.Detection.Method = "content (extension parser rejected file)"
	}
	if props.SampleRate == 0 || props.Channels == 0 {
		// TagLib "opens" some non-audio input (e.g. a zero-byte .mp3) as a valid
		// MPEG file with no stream. Treat that as malformed.
		return fail(fmt.Errorf("library accepted file as %q but found no audio stream (sample_rate=0)", r.Format))
	}
	if c := r.Detection.ByContent; !strings.HasPrefix(c, props.Format) && !strings.HasPrefix(c, "id3v2") {
		r.Warnings = append(r.Warnings, fmt.Sprintf("misdetection: content looks like %q but library parsed it as %q (chosen by extension)", c, r.Format))
	}
	r.LengthMs = props.Length.Milliseconds()
	r.SampleRate = props.SampleRate
	r.Channels = props.Channels

	for i, d := range props.Images {
		img, err := guarded("ReadImageOptions", func() ([]byte, error) { return taglib.ReadImageOptions(p, i) })
		if err != nil {
			return fail(err)
		}
		r.Pictures = append(r.Pictures, Picture{Type: d.Type, Description: d.Description, Mime: d.MIMEType, Bytes: len(img), SHA256: sha(img)})
	}

	r.Title = first(tags, "TITLE")
	r.Artist = first(tags, "ARTIST")
	r.Album = first(tags, "ALBUM")
	r.AlbumArtist = first(tags, "ALBUMARTIST")
	r.Year = atoiPtr(first(tags, "DATE"), leadInt)
	tn, dn := first(tags, "TRACKNUMBER"), first(tags, "DISCNUMBER")
	r.Track, r.Disc = atoiPtr(tn, leadInt), atoiPtr(dn, leadInt)
	r.TrackTotal, r.DiscTotal = atoiPtr(tn, slashTot), atoiPtr(dn, slashTot)
	if v := atoiPtr(first(tags, "TRACKTOTAL"), leadInt); v != nil {
		r.TrackTotal = v
	}
	if v := atoiPtr(first(tags, "DISCTOTAL"), leadInt); v != nil {
		r.DiscTotal = v
	}
	if g := tags["GENRE"]; g != nil {
		r.Genres = g
	}
	for k, v := range tags {
		if !coreKeys[k] {
			r.OtherTags[k] = v
		}
	}
	r.OK = true
	return r
}

func collect(paths []string) ([]string, error) {
	var out []string
	for _, p := range paths {
		st, err := os.Stat(p)
		if err != nil {
			out = append(out, p) // reported as a per-file error later
			continue
		}
		if !st.IsDir() {
			out = append(out, p)
			continue
		}
		err = filepath.WalkDir(p, func(q string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if !d.IsDir() {
				out = append(out, q)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	sort.Strings(out)
	return out, nil
}

func cmdRead(args []string) int {
	files, err := collect(args)
	if err != nil {
		return emit(map[string]any{"ok": false, "error": err.Error()}, 2)
	}
	res := make([]ReadResult, 0, len(files))
	for _, f := range files {
		res = append(res, readFile(f))
	}
	return emit(res, 0)
}
