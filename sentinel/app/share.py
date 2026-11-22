"""Public "viewer" link for presentations: runs a Cloudflare quick tunnel (cloudflared) to this app.

The tunnel gives an https://<random>.trycloudflare.com address that anyone can open. Requests that arrive
through it carry proxy headers, so server.py treats them as remote: read-only "viewer" without a PIN,
admin only with the PIN. No account is needed; the address changes every time the tunnel starts.
Tailscale Funnel works too: run `tailscale funnel 8000` yourself and paste the address in the share card.
"""
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

INSTALL_HINT = "cloudflared is not installed: open cmd and run  winget install --id Cloudflare.cloudflared  then restart the app"


def find_cloudflared():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if not base:
            continue
        for p in (pathlib.Path(base) / "cloudflared" / "cloudflared.exe",
                  pathlib.Path(base) / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe"):
            if p.exists():
                return str(p)
    return None


class Tunnel:
    def __init__(self):
        self.proc = None
        self.url = None
        self.error = None
        self.started = None
        self.lock = threading.Lock()

    def status(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            return {"running": running, "url": self.url if running else None, "error": self.error,
                    "installed": find_cloudflared() is not None, "started": self.started}

    def start(self, port):
        exe = find_cloudflared()
        if not exe:
            raise ValueError(INSTALL_HINT)
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self.url, self.error, self.started = None, None, time.time()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen(
                [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", creationflags=flags)
            proc = self.proc
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()

    def _read(self, proc):
        tail = []
        for line in proc.stdout:
            tail = (tail + [line.strip()])[-8:]
            m = URL_RE.search(line)
            if m:
                with self.lock:
                    if self.proc is proc:
                        self.url = m.group(0)
        code = proc.wait()
        with self.lock:
            if self.proc is proc and code not in (0, None) and not self.error:
                self.error = "cloudflared stopped: " + (" | ".join(t for t in tail if t)[-300:] or f"exit {code}")

    def stop(self):
        with self.lock:
            proc, self.proc, self.url = self.proc, None, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
