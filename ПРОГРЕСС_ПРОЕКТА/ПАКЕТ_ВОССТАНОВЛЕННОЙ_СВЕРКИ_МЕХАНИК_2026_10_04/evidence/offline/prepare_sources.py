"""Compatibility entry point: delegate exclusively to the safe package bootstrap."""
import argparse
from pathlib import Path
import runpy
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('repo', help='Existing checkout; read only')
args = parser.parse_args()
package = Path(__file__).absolute().parents[2]
bootstrap = package / 'prepare_sources.py'
sys.argv = [str(bootstrap), '--repo', args.repo, '--output', str(package)]
runpy.run_path(str(bootstrap), run_name='__main__')
