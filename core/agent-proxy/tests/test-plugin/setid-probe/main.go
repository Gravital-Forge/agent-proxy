// Command setid-probe tries, in one directory, each route to a file that holds
// the setuid or the setgid bit, then the controls that must still work there.
// It prints one line per attempt: the route, the mode, and ok or the errno's
// name.
package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"unsafe"
)

const (
	sysOpenat2      = 437
	sysFchmodat2    = 452
	oTmpfile        = 0o20000000 | syscall.O_DIRECTORY
	atSymlinkFollow = 0x400
	createFlags     = syscall.O_CREAT | syscall.O_WRONLY
)

// AT_FDCWD, which the syscall package does not export on Linux. A variable,
// because a negative constant does not convert to uintptr.
var cwd = -100

var errnoNames = map[syscall.Errno]string{
	syscall.EACCES:     "EACCES",
	syscall.EBADF:      "EBADF",
	syscall.EEXIST:     "EEXIST",
	syscall.EINVAL:     "EINVAL",
	syscall.EISDIR:     "EISDIR",
	syscall.ENOENT:     "ENOENT",
	syscall.ENOSYS:     "ENOSYS",
	syscall.ENOTDIR:    "ENOTDIR",
	syscall.EOPNOTSUPP: "EOPNOTSUPP",
	syscall.EPERM:      "EPERM",
	syscall.EROFS:      "EROFS",
}

type route struct {
	name string
	try  func(path string, mode uint32) error
}

// Each change route sets a mode on the existing target file.
var changeRoutes = []route{
	{"chmod", chmod},
	{"fchmod", fchmod},
	{"fchmodat", fchmodat},
	{"fchmodat2", fchmodat2},
}

// Each create route makes a new file at its path.
var createRoutes = []route{
	{"creat", creat},
	{"mknod", mknod},
	{"mknodat", mknodat},
	{"open", open},
	{"openat", openatCreate},
	{"openat-tmpfile", openatTmpfile},
	{"openat2", openat2},
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: setid-probe <directory>")
		os.Exit(2)
	}
	dir := os.Args[1]
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, []byte("probe\n"), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, mode := range []uint32{syscall.S_ISUID | 0o755, syscall.S_ISGID | 0o755} {
		for _, r := range changeRoutes {
			report(r.name, mode, r.try(target, mode))
		}
		for _, r := range createRoutes {
			path := filepath.Join(dir, fmt.Sprintf("%s-%04o", r.name, mode))
			report(r.name, mode, r.try(path, mode))
		}
	}
	control := filepath.Join(dir, "control-file")
	report("create", 0o755, openatCreate(control, 0o755))
	report("chmod", 0o755, chmod(control, 0o755))
	report("mkdir", 0o755, os.Mkdir(filepath.Join(dir, "control-dir"), 0o755))
	_, err := os.ReadFile(target)
	fmt.Println("read", "-", outcome(err))
}

func report(route string, mode uint32, err error) {
	fmt.Printf("%s %04o %s\n", route, mode, outcome(err))
}

func outcome(err error) string {
	if err == nil {
		return "ok"
	}
	var errno syscall.Errno
	if !errors.As(err, &errno) {
		return err.Error()
	}
	if name, known := errnoNames[errno]; known {
		return name
	}
	return "errno-" + strconv.Itoa(int(errno))
}

func failure(errno syscall.Errno) error {
	if errno == 0 {
		return nil
	}
	return errno
}

// closed takes a call's three results and closes the descriptor it returned.
func closed(fd, _ uintptr, errno syscall.Errno) error {
	if errno != 0 {
		return errno
	}
	return syscall.Close(int(fd))
}

func chmod(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall(syscall.SYS_CHMOD, uintptr(unsafe.Pointer(p)), uintptr(mode), 0)
	return failure(errno)
}

func fchmod(path string, mode uint32) error {
	fd, err := syscall.Open(path, syscall.O_RDONLY, 0)
	if err != nil {
		return err
	}
	defer syscall.Close(fd)
	_, _, errno := syscall.Syscall(syscall.SYS_FCHMOD, uintptr(fd), uintptr(mode), 0)
	return failure(errno)
}

func fchmodat(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall(syscall.SYS_FCHMODAT, uintptr(cwd), uintptr(unsafe.Pointer(p)), uintptr(mode))
	return failure(errno)
}

func fchmodat2(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall6(sysFchmodat2, uintptr(cwd), uintptr(unsafe.Pointer(p)), uintptr(mode), 0, 0, 0)
	return failure(errno)
}

func creat(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	return closed(syscall.Syscall(syscall.SYS_CREAT, uintptr(unsafe.Pointer(p)), uintptr(mode), 0))
}

func mknod(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall(syscall.SYS_MKNOD, uintptr(unsafe.Pointer(p)), uintptr(syscall.S_IFREG|mode), 0)
	return failure(errno)
}

func mknodat(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall6(syscall.SYS_MKNODAT, uintptr(cwd), uintptr(unsafe.Pointer(p)), uintptr(syscall.S_IFREG|mode), 0, 0, 0)
	return failure(errno)
}

func open(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	return closed(syscall.Syscall(syscall.SYS_OPEN, uintptr(unsafe.Pointer(p)), createFlags, uintptr(mode)))
}

func openat(path string, flags int, mode uint32) (uintptr, syscall.Errno) {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return 0, syscall.EINVAL
	}
	fd, _, errno := syscall.Syscall6(syscall.SYS_OPENAT, uintptr(cwd), uintptr(unsafe.Pointer(p)), uintptr(flags), uintptr(mode), 0, 0)
	return fd, errno
}

func openatCreate(path string, mode uint32) error {
	fd, errno := openat(path, createFlags, mode)
	return closed(fd, 0, errno)
}

// openatTmpfile opens an unnamed file in the path's directory, then links it
// at the path through /proc, because linkat with AT_EMPTY_PATH needs a
// capability the cage lacks.
func openatTmpfile(path string, mode uint32) error {
	fd, errno := openat(filepath.Dir(path), oTmpfile|syscall.O_WRONLY, mode)
	if errno != 0 {
		return errno
	}
	defer syscall.Close(int(fd))
	from, err := syscall.BytePtrFromString("/proc/self/fd/" + strconv.Itoa(int(fd)))
	if err != nil {
		return err
	}
	to, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	_, _, errno = syscall.Syscall6(syscall.SYS_LINKAT, uintptr(cwd), uintptr(unsafe.Pointer(from)), uintptr(cwd), uintptr(unsafe.Pointer(to)), atSymlinkFollow, 0)
	return failure(errno)
}

// openHow is the kernel's struct open_how.
type openHow struct {
	flags   uint64
	mode    uint64
	resolve uint64
}

func openat2(path string, mode uint32) error {
	p, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	how := openHow{flags: createFlags, mode: uint64(mode)}
	return closed(syscall.Syscall6(sysOpenat2, uintptr(cwd), uintptr(unsafe.Pointer(p)), uintptr(unsafe.Pointer(&how)), unsafe.Sizeof(how), 0, 0))
}
