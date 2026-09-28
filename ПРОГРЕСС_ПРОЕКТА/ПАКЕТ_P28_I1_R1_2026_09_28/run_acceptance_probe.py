"""Replay Astra's immutable C1/C2/C3 acceptance probe against R1."""

from pathlib import Path
import subprocess
import sys


package = Path(__file__).resolve().parent
progress = package.parent
probe = progress / 'ДОКАЗАТЕЛЬСТВА_ПРИЕМКИ_P28_I1_2026_09_28' / 'counterexamples.py'
log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else package / 'acceptance-replay-r1-utf8.log'
command = [sys.executable, '-B', str(probe), '--package', str(package)]
result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
output = result.stdout + result.stderr
summary = (
    f'P28_I1_R1_ACCEPTANCE_COMMAND={command!r}\n'
    f'P28_I1_R1_ACCEPTANCE_EXIT_CODE={result.returncode}\n'
)
log_path.write_text(output + summary, encoding='utf-8', newline='\n')
sys.stdout.write(output)
sys.stdout.write(summary)
raise SystemExit(result.returncode)
