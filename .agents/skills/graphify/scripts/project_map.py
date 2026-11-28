"""Project-local Codex entry point. Use the pinned Graphify CLI on backend sources."""
import argparse
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import tempfile

VERSION = "0.9.121"
EXTENSIONS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
SKIP = {"media", "private_media", "employee_photos", "node_modules", "staticfiles",
        "vendor", "backups", "migrations", ".venv", "__pycache__", "graphify-out"}
OUTPUTS = ("graph.json", "graph.html", "GRAPH_REPORT.md")


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args])


def read_sources(backend):
    listed = git(backend, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", ".")
    sources = {}
    for raw in sorted(set(listed.split(b"\0")) - {b""}):
        rel = Path(raw.decode("utf-8"))
        path = backend / rel
        if rel.suffix.lower() not in EXTENSIONS or SKIP.intersection(rel.parts):
            continue
        if path.is_file() and path.resolve().is_relative_to(backend) and not path.is_symlink():
            sources[rel.as_posix()] = path.read_bytes()
    return sources


def ensure_map(backend, cache, exe, env):
    sources = read_sources(backend)
    if not sources:
        raise SystemExit("В выбранной рабочей копии не найдены исходники backend.")
    head = git(backend, "rev-parse", "HEAD").decode().strip()
    digest = hashlib.sha256((VERSION + head).encode())
    for name, data in sources.items():
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    state = {"checkout": str(backend), "head": head, "fingerprint": digest.hexdigest(),
             "files": len(sources), "graphify": VERSION}
    marker = cache / "state.json"
    if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) == state and all(
        (cache / name).is_file() for name in OUTPUTS
    ):
        return
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        staging = Path(tmp)
        for name, data in sources.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        subprocess.run([exe, "update", "."], cwd=staging, env=env, check=True)
        output = staging / "graphify-out"
        if not all((output / name).is_file() for name in OUTPUTS):
            raise SystemExit("Graphify не создал полный комплект файлов карты.")
        marker.unlink(missing_ok=True)
        for name in OUTPUTS:
            (output / name).replace(cache / name)
        stamp = staging / "state.json"
        stamp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        stamp.replace(marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("action", choices=("update", "explain", "path", "query"))
    parser.add_argument("terms", nargs="*")
    args = parser.parse_args()
    if len(args.terms) != {"update": 0, "explain": 1, "query": 1, "path": 2}[args.action]:
        parser.error("Ожидается 0 аргументов для update, 1 для explain/query, 2 для path.")
    repo = Path(os.fsdecode(git(args.repo, "rev-parse", "--show-toplevel").strip())).resolve()
    backend = (repo / "СИСТЕМА_MVP" / "backend").resolve()
    if not (backend / "manage.py").is_file():
        parser.error("Укажите рабочую копию Copper Resources.")
    runtime = Path(os.environ["LOCALAPPDATA"]) / "CopperResources" / "Graphify"
    exe = runtime / "venv" / "Scripts" / "graphify.exe"
    if not exe.is_file():
        raise SystemExit(f"Установите graphifyy=={VERSION} в {runtime / 'venv'}.")
    cache = runtime / "maps" / hashlib.sha256(os.path.normcase(str(repo)).encode()).hexdigest()[:20]
    cache.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GRAPHIFY_OUT": "graphify-out", "PYTHONUTF8": "1", "GRAPHIFY_MAX_WORKERS": "2"}
    with (cache / "map.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            raise SystemExit("Карта этой рабочей копии занята. Повторите после завершения обновления.")
        ensure_map(backend, cache, exe, env)
        if args.action == "update":
            print(cache / "graph.html")
        else:
            cmd = [exe, args.action, *args.terms, "--graph", cache / "graph.json"]
            raise SystemExit(subprocess.run(cmd, cwd=backend, env=env).returncode)


if __name__ == "__main__":
    main()
