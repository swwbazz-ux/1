import subprocess
from pathlib import Path


def find_mvp_dir(project_root: Path) -> Path | None:
    candidates = [path for path in project_root.iterdir() if path.is_dir() and path.name.endswith("_MVP")]
    return candidates[0] if candidates else None


def main() -> int:
    project_root = Path(__file__).resolve().parent
    mvp_dir = find_mvp_dir(project_root)
    if mvp_dir:
        backend = mvp_dir / "backend"
        backend.mkdir(parents=True, exist_ok=True)
        (backend / ".server-stop").write_text("stop\n", encoding="utf-8")

    print("Stopping local server on port 8000...")
    output = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
    pids: set[str] = set()
    for line in output.stdout.splitlines():
        if ":8000" in line and "LISTENING" in line:
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
