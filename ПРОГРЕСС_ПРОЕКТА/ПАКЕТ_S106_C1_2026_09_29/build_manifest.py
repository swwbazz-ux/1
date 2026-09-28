import hashlib
from pathlib import Path


root = Path(__file__).resolve().parent
target = root / 'SHA256SUMS.txt'
rows = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or path == target or '__pycache__' in path.parts:
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows.append(f"{digest}  {path.relative_to(root).as_posix()}")
target.write_text('\n'.join(rows) + '\n', encoding='utf-8', newline='\n')
print(f'WROTE {target} ({len(rows)} files)')
