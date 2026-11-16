#!/usr/bin/env python3
"""SerialLink write behaviour against a pseudo-terminal: writes must wait
for room rather than fail on EAGAIN, and a device that accepts nothing
must raise SerialWriteStalled (which runners map to probe recovery)."""

import os
import pty
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infrastructure", "core"))
from serial_link import SerialLink, SerialWriteStalled  # noqa: E402


def test_stalled_device_raises():
    master, slave = pty.openpty()
    link = SerialLink(os.ttyname(slave))
    try:
        start = time.monotonic()
        try:
            for _ in range(10000):  # nobody reads the master: the queue fills
                link.send_raw(b"x" * 1024, stall_timeout=0.5)
        except SerialWriteStalled as e:
            assert "accepted no data" in str(e), e
            assert time.monotonic() - start < 30
        else:
            raise AssertionError("expected SerialWriteStalled")
    finally:
        link.close()
        os.close(master)
        os.close(slave)


def test_slow_reader_gets_everything():
    master, slave = pty.openpty()
    link = SerialLink(os.ttyname(slave))
    payload = bytes(range(256)) * 2048  # 512 KiB: far more than the tty queue
    received = bytearray()

    def drain():
        while len(received) < len(payload):
            time.sleep(0.01)
            received.extend(os.read(master, 65536))

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        link.send_raw(payload, stall_timeout=5)
        t.join(timeout=30)
        assert bytes(received) == payload, "lost or reordered bytes: got %d of %d" % (len(received), len(payload))
    finally:
        link.close()
        os.close(master)
        os.close(slave)


if __name__ == "__main__":
    test_stalled_device_raises()
    test_slow_reader_gets_everything()
    print("✓ All serial-link tests passed")
