"""Low-level serial I/O for the VisionCB HIL runner.

No pyserial dependency (not installed on this host, and the raw termios
configuration needed -- 115200 8N1, raw mode, no hardware flow control --
is a handful of stdlib calls). A background thread continuously drains
the file descriptor into an in-memory buffer, which is what makes
pattern-waiting reliable: the interactive manual sessions in Phases 6/7
hit repeated false negatives from `cat device > logfile &` being
block-buffered when not attached to a terminal, so a mid-loop `grep` on
the file saw stale/incomplete data even though the real bytes had
already arrived on the wire. A live buffer behind a lock has no such gap.
"""

import fcntl
import os
import re
import termios
import threading
import time


class SerialTimeout(Exception):
    pass


class SerialLink:
    def __init__(self, path, baud=115200, log_file=None):
        self.path = path
        self.log_file = log_file
        self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        self._configure_raw(baud)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def _configure_raw(self, baud):
        baud_map = {115200: termios.B115200}
        if baud not in baud_map:
            raise ValueError("unsupported baud rate: %r" % (baud,))
        b = baud_map[baud]

        attrs = termios.tcgetattr(self._fd)
        _iflag, _oflag, _cflag, _lflag, _ispeed, _ospeed, cc = attrs
        iflag = 0
        oflag = 0
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        lflag = 0
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, b, b, cc])

        # Match the proven `stty ... -hupcl` from every manual session:
        # do not drop DTR/assert a hangup on close, since this process
        # reopens the device repeatedly across independent runs.
        attrs = termios.tcgetattr(self._fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(self._fd, termios.TCSANOW, attrs)

        flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
        fcntl.fcntl(self._fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _reader_loop(self):
        while not self._stop.is_set():
            try:
                chunk = os.read(self._fd, 4096)
            except BlockingIOError:
                chunk = b""
            except OSError:
                break
            if chunk:
                with self._lock:
                    self._buf.extend(chunk)
                    if len(self._buf) > 2_000_000:
                        del self._buf[:-1_000_000]
                if self.log_file is not None:
                    with open(self.log_file, "ab") as f:
                        f.write(chunk)
            else:
                time.sleep(0.02)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        os.close(self._fd)

    def send_line(self, text):
        os.write(self._fd, text.encode("ascii") + b"\r\n")

    def send_raw(self, data: bytes):
        os.write(self._fd, data)

    def snapshot_text(self):
        with self._lock:
            return bytes(self._buf).decode("ascii", errors="replace")

    def clear_buffer(self):
        with self._lock:
            self._buf.clear()

    def wait_for(self, pattern, timeout, poll_interval=0.1):
        """Block until `pattern` (a compiled regex) is found anywhere in
        the accumulated buffer, or raise SerialTimeout."""
        deadline = time.monotonic() + timeout
        while True:
            text = self.snapshot_text()
            m = pattern.search(text)
            if m:
                return m, text
            if time.monotonic() >= deadline:
                raise SerialTimeout(
                    "timed out after %.1fs waiting for pattern %r; last %d bytes: %r"
                    % (timeout, pattern.pattern, min(len(text), 500), text[-500:])
                )
            time.sleep(poll_interval)

    def catch_uboot_prompt(self, timeout, cr_interval=0.12):
        """Send a bare CR/LF every `cr_interval` seconds while polling the
        live buffer for the U-Boot prompt. Same technique proven manually
        in Phases 6/7, but checked against a live, continuously-updated
        buffer instead of periodically re-reading a possibly stale log
        file, which is what caused repeated false negatives when this was
        done by hand."""
        pattern = re.compile(r"u-boot=>\s*$", re.MULTILINE)
        deadline = time.monotonic() + timeout
        self.clear_buffer()
        while True:
            self.send_raw(b"\r\n")
            text = self.snapshot_text()
            if pattern.search(text):
                return text
            if time.monotonic() >= deadline:
                raise SerialTimeout(
                    "timed out after %.1fs catching U-Boot prompt; last %d bytes: %r"
                    % (timeout, min(len(text), 500), text[-500:])
                )
            time.sleep(cr_interval)


def resolve_segger_jlink_device(serial_number, by_id_dir="/dev/serial/by-id"):
    """Resolve the VisionCB console through /dev/serial/by-id, matching
    the known SEGGER serial number rather than any ttyACM index (the
    J-Link's ttyACM number changed at least twice across Phases 6/7/8 in
    this same session -- by-id is the only stable identity). Fails
    loudly and specifically on absence or ambiguity."""
    if not os.path.isdir(by_id_dir):
        raise RuntimeError("%s does not exist -- no serial devices present at all" % by_id_dir)

    needle = "SEGGER_J-Link_%s" % serial_number
    matches = [os.path.join(by_id_dir, name) for name in os.listdir(by_id_dir) if needle in name]

    if len(matches) == 0:
        raise RuntimeError(
            "no /dev/serial/by-id entry matches %r; present entries: %s"
            % (needle, sorted(os.listdir(by_id_dir)))
        )
    if len(matches) > 1:
        raise RuntimeError("ambiguous: %d entries match %r: %s" % (len(matches), needle, matches))

    resolved = os.path.realpath(matches[0])
    if not os.path.exists(resolved):
        raise RuntimeError("resolved device %s does not exist" % resolved)
    return resolved
