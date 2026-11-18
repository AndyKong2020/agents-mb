"""Incremental transport and atomic local workspace checkout for mb."""

from collections import Counter
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen


CHUNK = 1024 * 1024
DIGEST = re.compile(r'[0-9a-f]{64}')


def paths(workspace, cache):
    workspace, cache = Path(workspace).resolve(), Path(cache).resolve()
    if workspace == cache or workspace in cache.parents or cache in workspace.parents:
        raise ValueError('workspace and cache directories must be disjoint')
    return workspace, cache


def regular(root, name):
    """Open a file relative to a fixed tree without following links in any component."""
    parts = name.split('/')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(root, flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(f'not a regular file: {name}')
        return os.fdopen(fd, 'rb')
    finally:
        os.close(parent)


def scan(root):
    tree, files = {}, {}

    def fail(exc):
        raise exc

    if not root.is_dir() or root.is_symlink():
        raise ValueError(f'expected a directory: {root}')
    for directory, folders, names, fd in os.fwalk(root, onerror=fail):
        for leaf in folders + names:
            name = str((Path(directory) / leaf).relative_to(root))
            info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            mode = info.st_mode & 0o777
            if stat.S_ISLNK(info.st_mode):
                tree[name] = ['l', os.readlink(leaf, dir_fd=fd)]
            elif stat.S_ISDIR(info.st_mode):
                tree[name] = ['d', mode]
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                size = 0
                with regular(root, name) as source:
                    for block in iter(lambda: source.read(CHUNK), b''):
                        digest.update(block)
                        size += len(block)
                key = digest.hexdigest()
                tree[name] = ['f', mode, key, size]
                files[key] = name
            else:
                raise ValueError(f'cannot sync special file: {name}')
    return tree, files


def manifest_sizes(tree):
    if not isinstance(tree, dict):
        raise ValueError('invalid sync manifest')
    sizes = {}
    for path, entry in tree.items():
        parts = path.split('/')
        if '\0' in path or any(p in ('', '.', '..') for p in parts):
            raise ValueError('invalid workspace-relative path')
        if len(parts) > 1 and (tree.get(path.rpartition('/')[0]) or [None])[0] != 'd':
            raise ValueError('entry parent must be a directory')
        if not isinstance(entry, list) or len(entry) < 2:
            raise ValueError('invalid workspace entry')
        kind, value = entry[:2]
        if kind == 'l' and len(entry) == 2 and isinstance(value, str) and value and '\0' not in value:
            continue
        if type(value) is not int or not 0 <= value <= 0o777:
            raise ValueError('invalid workspace mode')
        if kind == 'd' and len(entry) == 2:
            continue
        if kind != 'f' or len(entry) != 4:
            raise ValueError('invalid workspace entry')
        key, size = entry[2:]
        if not isinstance(key, str) or not DIGEST.fullmatch(key) or type(size) is not int or size < 0:
            raise ValueError('invalid blob metadata')
        if sizes.setdefault(key, size) != size:
            raise ValueError('inconsistent blob size')
    return sizes


def header(tree, send):
    body = json.dumps([tree, send], separators=(',', ':')).encode()
    return struct.pack('>Q', len(body)) + body


def read_header(stream, length):
    prefix = stream.read(8)
    if len(prefix) != 8:
        raise ValueError('incomplete sync header')
    size, = struct.unpack('>Q', prefix)
    if size > length - 8:
        raise ValueError('incomplete sync manifest')
    tree, send = json.loads(stream.read(size))
    sizes = manifest_sizes(tree)
    if not isinstance(send, list) or any(k not in sizes for k in send) or len(set(send)) != len(send):
        raise ValueError('invalid blob list')
    if length != 8 + size + sum(sizes[k] for k in send):
        raise ValueError('incorrect sync body length')
    return tree, send, sizes


def receive(stream, dest, key, size):
    digest = hashlib.sha256()
    with dest.open('xb') as out:
        while size:
            block = stream.read(min(size, CHUNK))
            if not block:
                raise ValueError('incomplete file transfer')
            digest.update(block)
            out.write(block)
            size -= len(block)
    if digest.hexdigest() != key:
        raise ValueError('file changed during sync')


@contextmanager
def request(url, token, rank, action, body=None, length=None):
    headers = {'Authorization': 'Bearer ' + token}
    if length is not None:
        headers['Content-Length'] = str(length)
    req = Request(f'{url.rstrip("/")}/sync/{int(rank)}/{action}', data=body, headers=headers)
    try:
        with urlopen(req) as response:
            yield response
    except HTTPException as exc:
        raise OSError(f'incomplete HTTP transfer: {exc}') from exc


def push(url, token, rank, workspace, cache):
    workspace, cache = paths(workspace, cache)
    tree, files = scan(workspace)
    sizes = manifest_sizes(tree)
    for attempt in range(2):
        with request(url, token, rank, 'inventory') as response:
            known = set(json.load(response))
        send = sorted(files.keys() - known)
        prefix = header(tree, send)

        def body():
            yield prefix
            for key in send:
                with regular(workspace, files[key]) as source:
                    remain = sizes[key]
                    while remain:
                        block = source.read(min(CHUNK, remain))
                        if not block:
                            raise ValueError('file shrank during sync')
                        remain -= len(block)
                        yield block

        try:
            with request(url, token, rank, 'push', body(), len(prefix) + sum(sizes[k] for k in send)):
                return
        except HTTPError as exc:
            if exc.code != 409 or attempt:
                raise
            exc.close()


def replace_tree(staged, target):
    if not os.path.lexists(target):
        os.rename(staged, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(staged), os.fsencode(target)
    if sys.platform == 'darwin':
        result = libc.renamex_np(old, new, 2)  # RENAME_SWAP
    else:
        result = libc.renameat2(-100, old, -100, new, 2)  # AT_FDCWD, RENAME_EXCHANGE
    if result:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(target))


def pull(url, token, rank, workspace, cache):
    _, cache = paths(workspace, cache)
    rank = str(int(rank))
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / rank
    with (cache / f'.mb-{rank}.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = scan(target)[1] if target.is_dir() and not target.is_symlink() else {}
        with tempfile.TemporaryDirectory(prefix=f'.mb-{rank}-', dir=cache) as tmp:
            tmp = Path(tmp)
            staged = tmp / 'workspace'
            staged.mkdir()
            with request(url, token, rank, 'pull', json.dumps(list(known)).encode()) as response:
                tree, send, sizes = read_header(response, int(response.headers['Content-Length']))
                for key in send:
                    receive(response, tmp / key, key, sizes[key])
            for key in sizes.keys() - set(send):
                with regular(target, known[key]) as source:
                    receive(source, tmp / key, key, sizes[key])
            remaining = Counter(entry[2] for entry in tree.values() if entry[0] == 'f')
            dirs = []
            for name in sorted(tree):
                kind, value, *rest = tree[name]
                path = staged / name
                if kind == 'd':
                    path.mkdir()
                    dirs.append((path, value))
                elif kind == 'l':
                    path.symlink_to(value)
                else:
                    key = rest[0]
                    remaining[key] -= 1
                    if remaining[key]:
                        shutil.copyfile(tmp / key, path)
                    else:
                        (tmp / key).rename(path)
                    path.chmod(value)
            for path, mode in reversed(dirs):
                path.chmod(mode)
            replace_tree(staged, target)
    return target
