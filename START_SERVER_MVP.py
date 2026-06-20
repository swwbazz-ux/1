import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


HOST = "127.0.0.1"
PORT = "8000"


def write_log(path: Path, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


def find_mvp_dir(project_root: Path) -> Path:
    candidates = [path for path in project_root.iterdir() if path.is_dir() and path.name.endswith("_MVP")]
    if not candidates:
        raise RuntimeError("MVP directory was not found.")
    return candidates[0]


def main() -> int:
    project_root = Path(__file__).resolve().parent
    mvp_dir = find_mvp_dir(project_root)
    backend = mvp_dir / "backend"
    site_packages = mvp_dir / ".venv" / "Lib" / "site-packages"
    stop_file = backend / ".server-stop"
    log_file = backend / "runserver.supervisor.log"

    if stop_file.exists():
        stop_file.unlink()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(site_packages)
    env["PYTHONUTF8"] = "1"

    print("")
    print("Starting MVP local server supervisor.")
    print(f"URL: http://{HOST}:{PORT}/")
    print("Keep this window open while using the site.")
    print("Stop with STOP_SERVER_MVP.bat or Ctrl+C.")
    print("")

    write_log(log_file, "supervisor started")

    migrate = subprocess.run(
        [sys.executable, "manage.py", "migrate"],
        cwd=backend,
        env=env,
    )
    if migrate.returncode != 0:
        write_log(log_file, f"migration failed with exit code {migrate.returncode}")
        print("Migration failed. Server was not started.")
        return migrate.returncode

    while True:
        if stop_file.exists():
            write_log(log_file, "stop marker found before server start")
            break

        write_log(log_file, "starting django runserver")
        process = subprocess.Popen(
            [sys.executable, "manage.py", "runserver", f"{HOST}:{PORT}", "--noreload"],
            cwd=backend,
            env=env,
        )
        exit_code = process.wait()
        write_log(log_file, f"django runserver stopped with exit code {exit_code}")

        if stop_file.exists():
            write_log(log_file, "stop marker found, supervisor exits")
            break

        print("")
        print("Django server stopped. Restarting in 2 seconds...")
        print("")
        time.sleep(2)

    print("Server supervisor stopped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("")
        print("Server supervisor interrupted.")
        raise SystemExit(0)
    except Exception as exc:
        print(f"Startup error: {exc}")
        raise SystemExit(1)
