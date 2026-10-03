"""Restore verified inputs from an existing Git repository, without changing tracked code."""
import argparse, hashlib, json, subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('repo',help='Path to a clone of swwbazz-ux/1 containing the pinned commit');args=p.parse_args()
base=Path(__file__).resolve().parent
manifest=json.loads((base/'source-manifest.json').read_text())
for row in manifest['files']:
    data=subprocess.run(['git','-C',args.repo,'show',manifest['release']+':'+row['path']],check=True,stdout=subprocess.PIPE).stdout
    assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()==row['git_blob_sha'],row['path']
    assert hashlib.sha256(data).hexdigest()==row['sha256'],row['path']
    target=base/row['local'];target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
print('Restored exact source files:',len(manifest['files']))
