// Command verify is a cage's entrypoint. It checks that each root mount point
// is the directory the runner recorded on the host, then replaces itself with
// the action.
//
// Usage: verify <nonce> [<root-path> <st_dev> <st_ino>]... -- <action argv...>
package main

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

const refused = 125

type root struct {
	path string
	dev  uint64
	ino  uint64
}

type invocation struct {
	nonce  string
	roots  []root
	action []string
}

func parse(args []string) (invocation, error) {
	if len(args) == 0 || args[0] == "" {
		return invocation{}, fmt.Errorf("no nonce")
	}
	inv := invocation{nonce: args[0]}
	rest := args[1:]
	for len(rest) > 0 && rest[0] != "--" {
		if len(rest) < 3 {
			return inv, fmt.Errorf("root %q: want <path> <st_dev> <st_ino>", rest[0])
		}
		r, err := parseRoot(rest[0], rest[1], rest[2])
		if err != nil {
			return inv, err
		}
		inv.roots = append(inv.roots, r)
		rest = rest[3:]
	}
	if len(rest) == 0 {
		return inv, fmt.Errorf("no -- before the action")
	}
	inv.action = rest[1:]
	if len(inv.action) == 0 || inv.action[0] == "" {
		return inv, fmt.Errorf("no action after --")
	}
	return inv, nil
}

func parseRoot(path, dev, ino string) (root, error) {
	if !strings.HasPrefix(path, "/") {
		return root{}, fmt.Errorf("root %q: path is not absolute", path)
	}
	d, err := strconv.ParseUint(dev, 10, 64)
	if err != nil {
		return root{}, fmt.Errorf("root %q: st_dev %q is not a decimal integer", path, dev)
	}
	i, err := strconv.ParseUint(ino, 10, 64)
	if err != nil {
		return root{}, fmt.Errorf("root %q: st_ino %q is not a decimal integer", path, ino)
	}
	return root{path: path, dev: d, ino: i}, nil
}

func check(roots []root) error {
	for _, r := range roots {
		var st syscall.Stat_t
		if err := syscall.Stat(r.path, &st); err != nil {
			return fmt.Errorf("%q: stat: %v", r.path, err)
		}
		// The conversions keep the build portable: st_dev is narrower than
		// 64 bits on some architectures.
		dev, ino := uint64(st.Dev), uint64(st.Ino)
		if dev != r.dev || ino != r.ino {
			return fmt.Errorf("%q: is %d:%d, want %d:%d", r.path, dev, ino, r.dev, r.ino)
		}
	}
	return nil
}

func fail(nonce, kind string, err error) {
	fmt.Fprintf(os.Stderr, "%s %s %v\n", nonce, kind, err)
	os.Exit(refused)
}

func main() {
	inv, err := parse(os.Args[1:])
	if err != nil {
		fail(inv.nonce, "root", err)
	}
	if err := check(inv.roots); err != nil {
		fail(inv.nonce, "root", err)
	}
	path, err := exec.LookPath(inv.action[0])
	if err != nil {
		fail(inv.nonce, "exec", err)
	}
	err = syscall.Exec(path, inv.action, os.Environ())
	fail(inv.nonce, "exec", fmt.Errorf("%q: %v", path, err))
}
