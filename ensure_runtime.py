"""Start local competition dependencies only when they are not already running."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from sync_cpolar_url import discover_url


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
LOGS = ROOT / "logs"


def ollama_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            json.loads(response.read().decode("utf-8"))
        return True
    except Exception:
        return False


def detached_flags() -> int:
    if os.name != "nt":
        return 0
    return (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def start_detached(command: list[str], log_name: str, pid_name: str) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / log_name, "ab", buffering=0)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=detached_flags(),
        close_fds=True,
    )
    (RUNTIME / pid_name).write_text(str(process.pid), encoding="ascii")
    return process.pid


def wait_until(check, seconds: int, label: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"{label} did not become ready within {seconds}s")


def find_ollama() -> str:
    candidates = [
        shutil.which("ollama"),
        str(Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("ollama executable not found")


def find_cpolar() -> str:
    candidates = [
        os.environ.get("CPOLAR_EXE", ""),
        shutil.which("cpolar") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("cpolar executable not found; set CPOLAR_EXE in .env")


def main() -> int:
    try:
        if ollama_ready():
            print("[OK  ] Ollama already running")
        else:
            pid = start_detached([find_ollama(), "serve"], "ollama.log", "ollama.pid")
            wait_until(ollama_ready, 30, "Ollama")
            print(f"[OK  ] Ollama started pid={pid}")

        try:
            public_url = discover_url()
            print(f"[OK  ] Optional public tunnel already running {public_url}")
        except Exception:
            try:
                pid = start_detached(
                    [find_cpolar(), "http", "5000", "-daemon=on", "-dashboard=on"],
                    "cpolar.log", "cpolar.pid",
                )
                wait_until(lambda: bool(discover_url()), 30, "Cpolar")
                print(f"[OK  ] Optional public tunnel started pid={pid} {discover_url()}")
            except Exception as tunnel_exc:
                print(f"[WARN] Public tunnel disabled: {tunnel_exc}")
                print("       Local Agent, demo replay and WebSocket remain available.")
        return 0
    except Exception as exc:
        print(f"[FAIL] Runtime dependency startup: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
