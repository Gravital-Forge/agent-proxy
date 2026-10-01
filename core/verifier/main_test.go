package main

import (
	"path/filepath"
	"reflect"
	"strconv"
	"syscall"
	"testing"
)

func TestParseAcceptsRootsAndAction(t *testing.T) {
	got, err := parse([]string{"n1", "/a", "10", "20", "/b", "30", "40", "--", "cat", "-n"})
	if err != nil {
		t.Fatal(err)
	}
	want := invocation{
		nonce:  "n1",
		roots:  []root{{"/a", 10, 20}, {"/b", 30, 40}},
		action: []string{"cat", "-n"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestParseAcceptsNoRoots(t *testing.T) {
	got, err := parse([]string{"n1", "--", "true"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got.roots) != 0 || !reflect.DeepEqual(got.action, []string{"true"}) {
		t.Fatalf("got %+v", got)
	}
}

func TestParseKeepsDashDashInsideTheAction(t *testing.T) {
	got, err := parse([]string{"n1", "--", "env", "--", "x"})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got.action, []string{"env", "--", "x"}) {
		t.Fatalf("got %+v", got.action)
	}
}

func TestParseRefuses(t *testing.T) {
	cases := map[string][]string{
		"no arguments":       {},
		"empty nonce":        {"", "--", "true"},
		"no separator":       {"n1", "/a", "1", "2", "true"},
		"short triple":       {"n1", "/a", "1", "--", "true"},
		"relative path":      {"n1", "a", "1", "2", "--", "true"},
		"negative dev":       {"n1", "/a", "-1", "2", "--", "true"},
		"hex ino":            {"n1", "/a", "1", "0x2", "--", "true"},
		"dev past 64 bits":   {"n1", "/a", "18446744073709551616", "2", "--", "true"},
		"no action":          {"n1", "/a", "1", "2", "--"},
		"empty action name":  {"n1", "--", ""},
		"nothing after root": {"n1", "/a", "1", "2"},
	}
	for name, args := range cases {
		if _, err := parse(args); err == nil {
			t.Errorf("%s: parse(%q) passed", name, args)
		}
	}
}

func pair(t *testing.T, path string) root {
	t.Helper()
	var st syscall.Stat_t
	if err := syscall.Stat(path, &st); err != nil {
		t.Fatal(err)
	}
	return root{path: path, dev: uint64(st.Dev), ino: uint64(st.Ino)}
}

func TestCheckPassesTheRecordedDirectories(t *testing.T) {
	a, b := t.TempDir(), t.TempDir()
	if err := check([]root{pair(t, a), pair(t, b)}); err != nil {
		t.Fatal(err)
	}
}

func TestCheckRefusesAnotherDirectory(t *testing.T) {
	a, b := t.TempDir(), t.TempDir()
	recorded := pair(t, b)
	recorded.path = a
	if err := check([]root{pair(t, a), recorded}); err == nil {
		t.Fatal("check passed a directory whose inode differs")
	}
}

func TestCheckRefusesAnotherDevice(t *testing.T) {
	a := t.TempDir()
	recorded := pair(t, a)
	recorded.dev++
	if err := check([]root{recorded}); err == nil {
		t.Fatal("check passed a directory whose device differs")
	}
}

func TestCheckRefusesAMissingRoot(t *testing.T) {
	recorded := pair(t, t.TempDir())
	recorded.path = filepath.Join(recorded.path, "gone")
	if err := check([]root{recorded}); err == nil {
		t.Fatal("check passed a missing path")
	}
}

func TestParseThenCheckRoundTrip(t *testing.T) {
	r := pair(t, t.TempDir())
	inv, err := parse([]string{
		"n1", r.path, strconv.FormatUint(r.dev, 10), strconv.FormatUint(r.ino, 10), "--", "true",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := check(inv.roots); err != nil {
		t.Fatal(err)
	}
}
