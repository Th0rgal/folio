import ctypes
import os
import select
import struct
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_ISDIR = 0x40000000

WATCH_MASK = (
    IN_MODIFY
    | IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
)

IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000


def _load_linux_inotify() -> Optional[Any]:
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        return libc
    except Exception:
        return None


_LIBC = _load_linux_inotify()


class NativeDirectoryWatcher:
    """Zero-idle-wakeup directory watcher backed by Linux `inotify` (with 65ms event coalescing),
    falling back to quiet interval polling on macOS/non-Linux platforms.
    """

    def __init__(
        self,
        roots_fn: Callable[[], Iterable[Path]],
        on_trigger: Callable[[], None],
        *,
        ignored_dirs: frozenset[str] = frozenset(),
        recursive: bool = True,
        fallback_interval_sec: float = 2.0,
        name: str = "folio-dir-watcher",
    ) -> None:
        self._roots_fn = roots_fn
        self._on_trigger = on_trigger
        self._ignored_dirs = ignored_dirs
        self._recursive = recursive
        self._fallback_interval = fallback_interval_sec
        self._name = name
        self._stop = threading.Event()
        self._wake_r: int = -1
        self._wake_w: int = -1
        self._ifd: int = -1
        self._wd_to_path: dict[int, Path] = {}
        self._path_to_wd: dict[Path, int] = {}
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            if _LIBC is not None:
                try:
                    ifd = _LIBC.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
                    if ifd >= 0:
                        r_fd, w_fd = os.pipe()
                        os.set_blocking(r_fd, False)
                        os.set_blocking(w_fd, False)
                        self._ifd = ifd
                        self._wake_r = r_fd
                        self._wake_w = w_fd
                        self._thread = threading.Thread(
                            target=self._run_inotify, daemon=True, name=self._name
                        )
                        self._thread.start()
                        return
                except Exception:
                    self._cleanup_fds()
            self._thread = threading.Thread(
                target=self._run_fallback_poll, daemon=True, name=self._name
            )
            self._thread.start()

    def notify_roots_changed(self) -> None:
        with self._lock:
            if self._wake_w >= 0:
                try:
                    os.write(self._wake_w, b"r")
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._wake_w >= 0:
                try:
                    os.write(self._wake_w, b"q")
                except OSError:
                    pass
            t = self._thread
        if t:
            t.join(timeout=2.0)
        with self._lock:
            self._cleanup_fds()

    def _cleanup_fds(self) -> None:
        for fd in (self._ifd, self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._ifd = -1
        self._wake_r = -1
        self._wake_w = -1
        self._wd_to_path.clear()
        self._path_to_wd.clear()

    def _add_watch_dir(self, directory: Path) -> None:
        if _LIBC is None or self._ifd < 0:
            return
        try:
            resolved = directory.resolve()
        except OSError:
            return
        if resolved in self._path_to_wd or not resolved.is_dir():
            return
        wd = _LIBC.inotify_add_watch(self._ifd, os.fsencode(str(resolved)), WATCH_MASK)
        if wd >= 0:
            self._wd_to_path[wd] = resolved
            self._path_to_wd[resolved] = wd

    def _sync_watches(self) -> None:
        for root in self._roots_fn():
            if not root.exists():
                if root.parent.exists():
                    self._add_watch_dir(root.parent)
                continue
            target_dir = root if root.is_dir() else root.parent
            self._add_watch_dir(target_dir)
            if self._recursive and root.is_dir():
                for dirpath, dirnames, _ in os.walk(root):
                    dirnames[:] = [
                        d for d in dirnames if d not in self._ignored_dirs and not d.startswith(".")
                    ]
                    self._add_watch_dir(Path(dirpath))

    def _drain_inotify_events(self) -> bool:
        fired = False
        new_dirs = False
        hdr_size = struct.calcsize("iIII")
        while self._ifd >= 0:
            try:
                buf = os.read(self._ifd, 65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if not buf:
                break
            offset = 0
            while offset + hdr_size <= len(buf):
                wd, mask, _cookie, name_len = struct.unpack_from("iIII", buf, offset)
                offset += hdr_size
                raw_name = buf[offset : offset + name_len].rstrip(b"\x00")
                offset += name_len
                name = os.fsdecode(raw_name) if raw_name else ""
                if name.startswith(".folio-") and name.endswith(".tmp"):
                    continue
                fired = True
                if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                    new_dirs = True
                if mask & (IN_DELETE_SELF | IN_MOVE_SELF):
                    old_path = self._wd_to_path.pop(wd, None)
                    if old_path:
                        self._path_to_wd.pop(old_path, None)
        if new_dirs:
            self._sync_watches()
        return fired

    def _run_inotify(self) -> None:
        self._sync_watches()
        poller = select.poll()
        poller.register(self._ifd, select.POLLIN)
        poller.register(self._wake_r, select.POLLIN)
        pending_debounce = False

        while not self._stop.is_set():
            timeout_ms = 65 if pending_debounce else None
            try:
                events = poller.poll(timeout_ms)
            except InterruptedError:
                continue
            except Exception:
                break

            if not events and pending_debounce:
                pending_debounce = False
                try:
                    self._on_trigger()
                except Exception:
                    pass
                continue

            for fd, _ev in events:
                if fd == self._wake_r:
                    try:
                        msg = os.read(self._wake_r, 64)
                    except OSError:
                        msg = b""
                    if b"q" in msg or self._stop.is_set():
                        return
                    self._sync_watches()
                elif fd == self._ifd:
                    if self._drain_inotify_events():
                        pending_debounce = True

    def _run_fallback_poll(self) -> None:
        while not self._stop.wait(self._fallback_interval):
            try:
                self._on_trigger()
            except Exception:
                pass
