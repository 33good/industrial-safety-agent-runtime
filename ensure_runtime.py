"""Ensure only the model service required by the personal local runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

from config import Settings


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
LOGS = ROOT / "logs"


def ollama_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=2) as response:
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


def start_detached(command: list[str]) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (LOGS / "ollama.log").open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=detached_flags(),
            close_fds=True,
        )
    (RUNTIME / "ollama.pid").write_text(str(process.pid), encoding="ascii")
    return process.pid


def find_ollama() -> str:
    candidates = [
        shutil.which("ollama"),
        str(Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("ollama executable not found")


def wait_until_ready(url: str, seconds: int = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ollama_ready(url):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Ollama did not become ready within {seconds}s")


def main() -> int:
    settings = Settings.from_env()
    if settings.llm_mode.lower() != "ollama":
        print(f"[SKIP] Ollama is not required for LLM_MODE={settings.llm_mode}")
        return 0
    try:
        if ollama_ready(settings.ollama_url):
            print("[OK  ] Ollama already running")
            return 0
        pid = start_detached([find_ollama(), "serve"])
        wait_until_ready(settings.ollama_url)
        print(f"[OK  ] Ollama started pid={pid}")
        return 0
    except Exception as exc:
        print(f"[FAIL] Ollama dependency startup: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
