"""Minimal KV260 serial-console driver (login, run command, logout)."""
import os, re, time, serial

class KV:
    def __init__(self, port="COM6"):
        self.s = serial.Serial(port, 115200, timeout=0.2)

    def read_until(self, pattern, timeout):
        buf, t0 = "", time.time()
        while time.time() - t0 < timeout:
            buf += self.s.read(4096).decode(errors="replace")
            m = re.search(pattern, buf)
            if m:
                return buf, m
        return buf, None

    def login(self, user, pw):
        self.s.reset_input_buffer()
        self.s.write(b"\r\n")
        buf, m = self.read_until(r"login: $|[$#] $", 5)
        if m and m.group(0).startswith("login"):
            self.s.write(user.encode() + b"\r\n")
            buf, m = self.read_until(r"[Pp]assword: ?$", 10)
            if not m:
                raise RuntimeError("no password prompt: " + buf[-200:])
            self.s.write(pw.encode() + b"\r\n")
            buf, m = self.read_until(r"[$#] $|incorrect", 30)
            if not m or "incorrect" in m.group(0):
                raise RuntimeError("login failed: " + buf[-200:])
        elif not m:
            raise RuntimeError("no prompt: " + buf[-200:])
        # quiet the console: no colour, wide terminal, no kernel messages concern
        self.run("export TERM=dumb; stty cols 400 -echo", 5)

    def run(self, cmd, timeout=30):
        self.s.reset_input_buffer()
        self.s.write((cmd + "; echo __DONE_$?__\r\n").encode())
        buf, m = self.read_until(r"__DONE_(\d+)__", timeout)
        if not m:
            raise RuntimeError(f"timeout running {cmd!r}: {buf[-300:]}")
        out = buf[:m.start()]
        out = out.replace(cmd + "; echo __DONE_$?__", "")
        return int(m.group(1)), out.strip("\r\n ")

    def logout(self):
        self.s.write(b"stty echo; exit\r\n")
        time.sleep(1)
        self.s.close()


def put_file(kv, local, remote, chunk=900):
    """Copy a local file to the board through the console (base64 in short lines)."""
    import base64, hashlib
    data = open(local, "rb").read()
    b64 = base64.b64encode(data).decode()
    kv.run("rm -f /tmp/_put.b64", 10)
    for i in range(0, len(b64), chunk):
        kv.run(f"echo {b64[i:i + chunk]} >> /tmp/_put.b64", 10)
    rc, out = kv.run(f"base64 -d /tmp/_put.b64 > {remote} && sha256sum {remote} | cut -c1-64; rm -f /tmp/_put.b64", 20)
    ok = out.strip().splitlines()[-1] == hashlib.sha256(data).hexdigest()
    return ok


def stream(kv, cmd, timeout, on_line):
    """Run a command and call on_line(text, host_time) for every output line as it arrives."""
    import re, time
    kv.s.reset_input_buffer()
    kv.s.write((cmd + "; echo __DONE_$?__\r\n").encode())
    buf, t0 = "", time.time()
    while time.time() - t0 < timeout:
        buf += kv.s.read(4096).decode(errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip("\r ")
            m = re.search(r"__DONE_(\d+)__", line)
            if m and "echo __DONE_" not in line:
                return int(m.group(1))
            if line:
                on_line(line, time.time())
    raise RuntimeError(f"timeout streaming {cmd!r}")
