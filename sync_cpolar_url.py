"""Synchronize PUBLIC_URL from the locally running cpolar inspector."""
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
INSPECTOR_PORTS = (4042, 4040)


def discover_url() -> str:
    errors = []
    patterns = (
        r'\\"PublicUrl\\":\\"(https://[^"\\]+)',
        r'"PublicUrl":"(https://[^"]+)',
    )
    for port in INSPECTOR_PORTS:
        url = f"http://127.0.0.1:{port}/http/in"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            candidates = []
            for pattern in patterns:
                candidates.extend(re.findall(pattern, html))
            for candidate in candidates:
                candidate = candidate.replace("\\/", "/")
                host = (urllib.parse.urlparse(candidate).hostname or "").lower()
                if host.endswith("cpolar.cn") or host.endswith("cpolar.top"):
                    return candidate.rstrip("/")
            errors.append(f"{port}:no_https_tunnel")
        except Exception as exc:
            errors.append(f"{port}:{type(exc).__name__}")
    raise RuntimeError("cpolar inspector unavailable (" + ", ".join(errors) + ")")


def update_env(public_url: str) -> bool:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    pattern = re.compile(r"(?m)^PUBLIC_URL=.*$")
    replacement = f"PUBLIC_URL={public_url}"
    if pattern.search(text):
        updated = pattern.sub(replacement, text, count=1)
    else:
        updated = text.rstrip("\r\n") + "\n" + replacement + "\n"
    if updated == text:
        return False
    ENV_PATH.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    optional = "--optional" in sys.argv[1:]
    try:
        public_url = discover_url()
        changed = update_env(public_url)
        print(f"[OK  ] Cpolar PUBLIC_URL   {public_url}{' (updated)' if changed else ''}")
        return 0
    except Exception as exc:
        label = "WARN" if optional else "FAIL"
        print(f"[{label}] Optional PUBLIC_URL unavailable: {exc}")
        if not optional:
            print("       Start 'cpolar http 5000' or pass --optional.")
        return 0 if optional else 1


if __name__ == "__main__":
    sys.exit(main())
