// Command cgoprobe briefly assesses cgo bindings to the system TagLib C API (tag_c).
// usage: cgoprobe genres <file> A;B;C   |   cgoprobe props <file> [forceflac]
package main

/*
#cgo pkg-config: taglib_c
#include <stdlib.h>
#include <taglib/tag_c.h>
*/
import "C"

import (
	"fmt"
	"os"
	"strings"
	"unsafe"
)

func strs(pp **C.char) []string {
	var out []string
	if pp == nil {
		return out
	}
	for _, p := range unsafe.Slice(pp, 1<<16) {
		if p == nil {
			break
		}
		out = append(out, C.GoString(p))
	}
	return out
}

func main() {
	C.taglib_set_strings_unicode(1)
	cp := C.CString(os.Args[2])
	defer C.free(unsafe.Pointer(cp))
	var f *C.TagLib_File
	if len(os.Args) > 3 && os.Args[3] == "forceflac" {
		f = C.taglib_file_new_type(cp, C.TagLib_File_FLAC)
	} else {
		f = C.taglib_file_new(cp)
	}
	if f == nil || C.taglib_file_is_valid(f) == 0 {
		fmt.Println("invalid file")
		os.Exit(1)
	}
	defer C.taglib_file_free(f)
	switch os.Args[1] {
	case "props":
		keys := C.taglib_property_keys(f)
		for _, k := range strs(keys) {
			ck := C.CString(k)
			v := C.taglib_property_get(f, ck)
			fmt.Printf("%s=%q\n", k, strs(v))
			C.taglib_property_free(v)
			C.free(unsafe.Pointer(ck))
		}
		C.taglib_property_free(keys)
		ap := C.taglib_file_audioproperties(f)
		fmt.Println("samplerate", C.taglib_audioproperties_samplerate(ap))
	case "genres":
		ck := C.CString("GENRE")
		defer C.free(unsafe.Pointer(ck))
		C.taglib_property_set(f, ck, nil)
		for _, g := range strings.Split(os.Args[3], ";") {
			cv := C.CString(g)
			C.taglib_property_set_append(f, ck, cv)
			C.free(unsafe.Pointer(cv))
		}
		fmt.Println("save:", C.taglib_file_save(f) != 0)
	}
}
