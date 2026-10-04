#!/usr/bin/env python3
"""Restore pinned probe inputs in a copied proof package, using read-only git show.

Uses POSIX directory handles and O_NOFOLLOW. Run on Linux/macOS or WSL.
No fetch, checkout, branch mutation, tracked-file write, or production access.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

PINNED_COMMIT = 'ecb61af55b699a7a2417abba6df4dd490f3464a9'
GROUPS = {'offline', 'shifts', 'trips', 'downtimes', 'assignments'}


def fail(message):
    raise ValueError(message)


def checked_absolute(value):
    path = Path(os.path.abspath(os.fspath(value)))
    for part in reversed((path, *path.parents)):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            fail(f'Symlink is forbidden: {part}')
    return path


def relative_parts(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\\\x00\r\n:'):
        fail('Invalid relative path in source-plan.json')
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(p in ('', '.', '..') for p in path.parts):
        fail(f'Unsafe relative path: {value!r}')
    return path.parts


def digest_check(data, entry):
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if len(data) != entry['bytes'] or blob != entry['git_blob_sha'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
        fail(f'Content/hash mismatch: {entry["repository_path"]}')


def directory_at(parent_fd, name, *, create=False):
    if create:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def parent_at(root_fd, parts, *, create=False):
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = directory_at(current, part, create=create)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def read_at(root_fd, parts):
    try:
        parent = parent_at(root_fd, parts)
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                fail('Destination is not a regular file')
            return handle.read()
    finally:
        os.close(parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='Existing checkout containing the pinned commit; read only')
    parser.add_argument('--output', default=str(Path(__file__).absolute().parent),
                        help='Existing copied proof package outside every Git checkout (default: script directory)')
    args = parser.parse_args()
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        fail('Secure directory-handle operations required; use Linux/macOS or WSL')
    package = checked_absolute(Path(__file__).absolute().parent)
    plan_path = checked_absolute(package / 'source-plan.json')
    if not stat.S_ISREG(plan_path.stat().st_mode):
        fail('source-plan.json must be a regular file')
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    if plan.get('schema') != 1 or not isinstance(plan.get('sources'), list) or not plan['sources']:
        fail('Unsupported or empty source plan')
    repo = checked_absolute(args.repo)
    output = checked_absolute(args.output)
    if not repo.is_dir() or not output.is_dir():
        fail('Repo and copied output package must already exist')
    for parent in (output, *output.parents):
        if os.path.lexists(parent / '.git'):
            fail('Output must be outside all Git checkouts; copy the proof package first')
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1', GIT_PAGER='cat')
    def git(*arguments):
        return subprocess.run(['git', '--no-pager', '-C', str(repo), *arguments],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env).stdout
    checkout = checked_absolute(git('rev-parse', '--show-toplevel').decode().strip())
    if output == checkout or checkout in output.parents:
        fail('Output is inside the supplied checkout')
    root_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if read_at(root_fd, ('source-plan.json',)) != plan_bytes:
            fail('Output must contain an identical source-plan.json from this proof package')
        staged, cache, destinations = [], {}, set()
        # Complete preflight of every path, Git blob, byte count and existing target before writing.
        for entry in plan['sources']:
            relative_parts(entry.get('repository_path'))
            parts = relative_parts(entry.get('destination'))
            if len(parts) < 4 or parts[0] != 'evidence' or parts[1] not in GROUPS or parts[2] != 'source':
                fail('Destination must remain in evidence/<group>/source/')
            if entry.get('commit') != PINNED_COMMIT:
                fail('Unexpected source commit')
            if not re.fullmatch(r'[0-9a-f]{40}', entry.get('git_blob_sha', '')) or not re.fullmatch(r'[0-9a-f]{64}', entry.get('sha256', '')):
                fail('Invalid hash in source plan')
            if type(entry.get('bytes')) is not int or entry['bytes'] < 0:
                fail('Invalid byte count')
            if parts in destinations:
                fail('Duplicate destination')
            destinations.add(parts)
            key = (entry['commit'], entry['repository_path'])
            if key not in cache:
                cache[key] = git('show', '--no-ext-diff', '--no-textconv', key[0] + ':' + key[1])
            data = cache[key]
            digest_check(data, entry)
            existing = read_at(root_fd, parts)
            if existing is not None and existing != data:
                fail(f'Refusing to overwrite different content: {entry["destination"]}')
            staged.append((parts, data, existing is not None))
        created = 0
        for parts, data, exists in staged:
            if exists:
                continue
            parent = parent_at(root_fd, parts, create=True)
            try:
                try:
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
                except FileExistsError:
                    if read_at(root_fd, parts) != data:
                        fail('Destination changed during preparation; nothing was overwritten')
                    continue
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                created += 1
            finally:
                os.close(parent)
        print(json.dumps({'verified_destinations': len(staged), 'unique_git_paths_read': len(cache),
                          'created_files': created, 'commit': PINNED_COMMIT,
                          'scope': 'Input restoration only; no domain or product test executed'}, ensure_ascii=False))
    finally:
        os.close(root_fd)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Preparation refused: {error}')
