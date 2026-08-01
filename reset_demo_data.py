"""Safely reset generated demo data before an on-site presentation."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GENERATED_FILES = {
    ROOT / "alarms": ("*.jpg", "*.jpeg", "*.png"),
    ROOT / "data" / "pending": ("*.json",),
    ROOT / "data" / "reports": ("report_*.md", "event_log.txt"),
    ROOT / "data" / "executions": ("*.json",),
}
DATABASE_FILES = (
    ROOT / "data" / "alarms.db",
    ROOT / "data" / "alarms.db-wal",
    ROOT / "data" / "alarms.db-shm",
    ROOT / "data" / "alarms.db-journal",
)
SERVICE_PORTS = (5000, 5001, 8080)


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def collect_targets() -> list[Path]:
    targets: set[Path] = {path for path in DATABASE_FILES if path.is_file()}
    for directory, patterns in GENERATED_FILES.items():
        if not directory.is_dir():
            continue
        for pattern in patterns:
            targets.update(path for path in directory.glob(pattern) if path.is_file())
    return sorted(targets, key=lambda path: str(path).lower())


def relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="清理线下演示产生的报警、审批、报告和执行记录")
    parser.add_argument("--yes", action="store_true", help="跳过 RESET 确认（用于自动化验收）")
    parser.add_argument("--dry-run", action="store_true", help="只显示将清理的内容")
    args = parser.parse_args()

    active_ports = [port for port in SERVICE_PORTS if port_is_open(port)]
    if active_ports:
        print(f"[FAIL] 系统仍在运行（端口：{', '.join(map(str, active_ports))}）。")
        print("请先双击 stop.bat，再重新运行 reset.bat。")
        return 2

    targets = collect_targets()
    total_bytes = sum(path.stat().st_size for path in targets)
    print("演示数据清理预览")
    print(f"  文件数量: {len(targets)}")
    print(f"  总大小:   {total_bytes / 1024 / 1024:.2f} MB")
    for path in targets:
        print(f"  - {relative(path)}")

    print("\n保留内容: data/backup、回放素材、模型、前端、配置和源代码")
    if args.dry_run:
        print("[OK] 仅预览，未删除任何文件。")
        return 0
    if not targets:
        print("[OK] 当前已经是干净状态。")
        return 0

    if not args.yes:
        answer = input("\n确认清理请输入 RESET: ").strip()
        if answer != "RESET":
            print("[CANCEL] 已取消，未删除任何文件。")
            return 1

    failures: list[tuple[Path, Exception]] = []
    for path in targets:
        try:
            path.unlink()
        except OSError as exc:
            failures.append((path, exc))

    if failures:
        for path, exc in failures:
            print(f"[FAIL] {relative(path)}: {exc}")
        print("清理未完全完成，请确认系统已经停止后重试。")
        return 3

    remaining = collect_targets()
    if remaining:
        print(f"[FAIL] 验收失败，仍有 {len(remaining)} 个运行数据文件。")
        return 4

    print(f"[OK] 已清理 {len(targets)} 个运行数据文件。")
    print("[OK] 下次启动时系统会自动创建空数据库和所需目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
