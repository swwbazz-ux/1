import os
import subprocess
from pathlib import Path


def find_mvp_dir(project_root: Path) -> Path | None:
    candidates = [path for path in project_root.iterdir() if path.is_dir() and path.name.endswith("_MVP")]
    return candidates[0] if candidates else None


def main() -> int:
    port = os.environ.get("MVP_SERVER_PORT", "8000").strip() or "8000"
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError("MVP_SERVER_PORT must be a TCP port between 1 and 65535.")
    project_root = Path(__file__).resolve().parent
    mvp_dir = find_mvp_dir(project_root)
    if mvp_dir:
        backend = mvp_dir / "backend"
        backend.mkdir(parents=True, exist_ok=True)
        (backend / ".server-stop").write_text("stop\n", encoding="utf-8")

    print(f"Stopping local server on port {port}...")
    output = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
    pids: set[str] = set()
    for line in output.stdout.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                pids.add(parts[-1])

    for pid in sorted(pids):
        print(f"Stopping process {pid}")
        subprocess.run(["taskkill", "/PID", pid, "/F"])

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
