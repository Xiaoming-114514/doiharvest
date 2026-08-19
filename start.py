#!/usr/bin/env python3
"""
DoiHarvest — One-Click Launcher
================================
Auto-detects project root, installs dependencies, and starts the web dashboard.

Usage:
    python start.py              # Start the web server
    python start.py --no-browser # Start without opening browser
    python start.py --port 8080  # Use custom port
"""

import os
import sys
import subprocess
import shutil
import time
import threading
import webbrowser
import socket
from pathlib import Path

# ── Project root detection ──────────────────────────────────
# Uses __file__ so this script works no matter where it's called from.
PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(str(PROJECT_ROOT))

# ── Python runtime detection ─────────────────────────────────
def find_python() -> str:
    """Find a usable Python for running subprocesses.

    Priority:
      1. Project virtual env (.venv created by install.py).
      2. The interpreter currently running this script (sys.executable) —
         guaranteed usable, since start.py is already running under it.
      3. System Python, skipping Microsoft Store App Execution Aliases
         (WindowsApps\\python*.exe placeholders that are NOT real Pythons).
    """
    # 1. Project venv (created by install.py)
    venv_py = PROJECT_ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if venv_py.exists():
        return str(venv_py)

    # 2. Current interpreter (the one running this script)
    if sys.executable:
        return sys.executable

    # 3. System python — skip Store aliases, verify it actually runs
    for cmd in ["python", "python3"]:
        path = shutil.which(cmd)
        if not path:
            continue
        # Microsoft Store App Execution Alias (WindowsApps\python*.exe) is a
        # placeholder that only opens the Store — not a real Python.
        if os.name == "nt" and "WindowsApps" in str(path):
            continue
        try:
            r = subprocess.run([path, "-c", "pass"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return path
        except Exception:
            continue

    print("[!] ERROR: No usable Python found.")
    print("    Run install.bat / install.py first, or install Python 3.10+.")
    input("Press Enter to exit...")
    sys.exit(1)

# ── Dependency check & install ───────────────────────────────
def install_deps(python_path: str) -> bool:
    """Check if required packages are installed, install if missing."""
    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.exists():
        print("[!] requirements.txt not found!")
        return False

    required = {}
    with open(req_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                pkg = line.split(">=")[0].split("==")[0].split("~=")[0].strip()
                required[pkg.lower()] = line

    # Check what's missing
    missing = []
    for pkg_name in required:
        result = subprocess.run(
            [python_path, "-c", f"import {pkg_name.replace('-','_')}"],
            capture_output=True
        )
        if result.returncode != 0:
            missing.append(required[pkg_name])

    if missing:
        print(f"[*] Installing {len(missing)} missing packages ...")
        for pkg in missing:
            print(f"    {pkg}")
        # Use Tsinghua mirror first for speed, fall back to official PyPI
        result = subprocess.run(
            [python_path, "-m", "pip", "install"] + missing +
            ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print("[*] Mirror install failed, retrying with official PyPI ...")
            result = subprocess.run(
                [python_path, "-m", "pip", "install"] + missing,
                capture_output=True, text=True
            )
        if result.returncode != 0:
            print(result.stderr[-500:] if result.stderr else "Unknown pip error")
            print("[!] WARNING: Some packages may not have installed. Trying to continue ...")
        else:
            print("[+] All dependencies installed.")
    else:
        print("[+] All dependencies already installed.")

    return True

# ── Node.js check (for Phase 2) ──────────────────────────────
def check_node() -> bool:
    """Check if Node.js is available for nature-downloader."""
    result = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"[+] Node.js {result.stdout.strip()} detected — Phase 2 ready.")
        return True
    else:
        print("[!] Node.js not found — Phase 2 (nature-downloader) will be unavailable.")
        print("    Install from: https://nodejs.org/")
        return False


def port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def kill_port_process(port: int):
    """Kill any process currently occupying the given port."""
    killed = False
    # Method 1: PowerShell (most reliable on Windows)
    if os.name == "nt":
        try:
            ps_cmd = (
                f"$c = Get-NetTCPConnection -LocalPort {port} -EA SilentlyContinue "
                f"| Where-Object {{ $_.State -eq 'Listen' }}; "
                f"foreach ($x in $c) {{ Stop-Process -Id $x.OwningProcess -Force -EA SilentlyContinue; "
                f"Write-Output $x.OwningProcess }}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15,
            )
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                if line and line.isdigit():
                    print(f"[*] Killed old process (PID {line}) on port {port}")
                    killed = True
            if killed:
                time.sleep(1)  # let OS release the port
                return
        except Exception:
            pass

    # Method 2: netstat + taskkill fallback
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and f":{port}" in parts[1] and "LISTEN" in parts[3].upper():
                pid_str = parts[-1]
                try:
                    pid = int(pid_str)
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                    print(f"[*] Killed old process (PID {pid}) on port {port}")
                    killed = True
                except (ValueError, OSError):
                    pass
        if killed:
            time.sleep(1)
    except Exception:
        pass


def start_uvicorn(python_path: str, port: int) -> subprocess.Popen:
    """Launch uvicorn in a subprocess and return the Popen handle."""
    proc = subprocess.Popen(
        [python_path, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return proc


def stream_output(proc: subprocess.Popen):
    """Print server output line by line in a background thread."""
    try:
        for line in proc.stdout:
            print(line, end="", flush=True)
    except Exception:
        pass


# ── Main ─────────────────────────────────────────────────────
def main():
    # Parse optional CLI args
    port = 8765
    open_browser = True
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("--port", "-p") and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--no-browser":
            open_browser = False
            i += 1
        else:
            i += 1

    print("=" * 60)
    print("  DoiHarvest — Phase 1+2 文献批量下载流水线")
    print("=" * 60)
    print(f"  Project root: {PROJECT_ROOT}")
    print()

    # Step 1: Find Python
    python_path = find_python()
    print(f"[*] Python: {python_path}")

    # Step 2: Install deps
    if not install_deps(python_path):
        input("Press Enter to exit...")
        sys.exit(1)

    # Step 3: Check Node (non-blocking)
    node_ok = check_node()

    print()
    print("-" * 60)
    print(f"  Starting web dashboard ...")
    print("-" * 60)
    print()

    # Step 4: Kill any old process hogging the port, then start uvicorn
    if port_is_open("127.0.0.1", port):
        print(f"[!] Port {port} is already in use. Killing old process ...")
        kill_port_process(port)
        if port_is_open("127.0.0.1", port):
            print(f"[!] Failed to free port {port}. Please close the old instance manually.")
            input("Press Enter to exit...")
            sys.exit(1)
        print(f"[+] Port {port} freed.")
    proc = start_uvicorn(python_path, port)

    # Start output streaming thread
    output_thread = threading.Thread(target=stream_output, args=(proc,), daemon=True)
    output_thread.start()

    # Wait for server to be ready (with timeout)
    dashboard_url = f"http://127.0.0.1:{port}"
    max_wait = 15  # seconds
    print(f"[*] Waiting for server to start on {dashboard_url} ...")
    for _ in range(max_wait * 2):  # check every 0.5s
        if proc.poll() is not None:
            # Server exited prematurely — show remaining output
            print(f"\n[!] Server exited with code {proc.returncode}. Check above for errors.")
            input("Press Enter to exit...")
            sys.exit(1)
        if port_is_open("127.0.0.1", port):
            break
        time.sleep(0.5)
    else:
        print(f"\n[!] Server did not start within {max_wait}s. It may have crashed silently.")
        print("    Check the output above for error messages.")
        proc.terminate()
        input("Press Enter to exit...")
        sys.exit(1)

    print(f"[+] Server is ready at {dashboard_url}")
    print("    Press Ctrl+C in this window to stop the server.")
    print()

    # Step 5: NOW open browser (server is confirmed running)
    if open_browser:
        webbrowser.open(dashboard_url)

    # Step 6: Wait for server to exit (or user presses Ctrl+C)
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[!] Stopping server ...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("[+] Server stopped.")

if __name__ == "__main__":
    main()
