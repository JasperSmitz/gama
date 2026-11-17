package main

import (
	"reflect"
	"testing"
)

func TestApplyChangesAndDiff(t *testing.T) {
	before := map[string][]string{"GENRE": {"Rock"}, "TITLE": {"x"}, "OLD": {"y"}}
	got := applyChanges(before, map[string][]string{"GENRE": {"A", "B"}, "OLD": nil})
	want := map[string][]string{"GENRE": {"A", "B"}, "TITLE": {"x"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("applyChanges = %v", got)
	}
	if d := diffTags(want, got); len(d) != 0 {
		t.Fatalf("diff should be empty: %v", d)
	}
	if d := diffTags(want, before); len(d) != 2 {
		t.Fatalf("expected 2 diffs, got %v", d)
	}
	if before["GENRE"][0] != "Rock" {
		t.Fatal("applyChanges mutated its input")
	}
}

func TestFieldChangesPerFormat(t *testing.T) {
	if ch := fieldChanges(ReadResult{Format: "flac"}); ch["TRACKTOTAL"][0] != "12" || ch["TRACKNUMBER"][0] != "7" {
		t.Fatalf("vorbis: %v", ch)
	}
	if ch := fieldChanges(ReadResult{Format: "mp4/aac"}); ch["TRACKNUMBER"][0] != "7/12" || ch["TRACKTOTAL"] != nil {
		t.Fatalf("mp4: %v", ch)
	}
}

func TestSplitAndParse(t *testing.T) {
	if g := splitList(" A; B;;C "); !reflect.DeepEqual(g, []string{"A", "B", "C"}) {
		t.Fatal(g)
	}
	if v := atoiPtr("7/12", slashTot); v == nil || *v != 12 {
		t.Fatal("slash total")
	}
	if v := atoiPtr("garbage", leadInt); v != nil {
		t.Fatal("expected nil")
	}
}
