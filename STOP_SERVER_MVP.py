import os
import subprocess
from pathlib import Path


ALLOWED_PORTS = {"8000", "8002"}


def selected_port() -> str:
    port = os.getenv("DJANGO_RUNSERVER_PORT", "8000").strip()
    if port not in ALLOWED_PORTS:
        raise RuntimeError("Only the standard port 8000 or isolated QA port 8002 is allowed.")
    return port


def find_mvp_dir(project_root: Path) -> Path | None:
    candidates = [path for path in project_root.iterdir() if path.is_dir() and path.name.endswith("_MVP")]
    return candidates[0] if candidates else None


def main() -> int:
    port = selected_port()
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
