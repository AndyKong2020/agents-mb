"""Store each rank's last complete workspace and stream content-addressed deltas."""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from sync_client import header, manifest_sizes, read_header, receive


def reply(handler, status, value):
    body = json.dumps(value, separators=(',', ':')).encode()
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle(handler, authenticated_rank):
    match = re.fullmatch(r'/sync/(-?(?:0|[1-9][0-9]*))/(inventory|push|pull)', handler.path)
    if not match:
        return reply(handler, 404, {'error': 'unknown sync endpoint'})
    rank, action = match.groups()
    if action != 'pull' and rank != str(authenticated_rank):
        return reply(handler, 403, {'error': 'workspace belongs to another rank'})
    if handler.command != ('GET' if action == 'inventory' else 'POST'):
        return reply(handler, 405, {'error': 'unsupported method'})
    root = Path(os.environ['MB_SYNC_DIR']) / rank
    manifest = root / 'manifest.json'
    if action == 'pull' and not manifest.exists():
        return reply(handler, 404, {'error': 'workspace has not been pushed'})
    root.mkdir(parents=True, exist_ok=True)
    blobs = root / 'blobs'
    blobs.mkdir(exist_ok=True)

    def snapshot():
        return json.loads(manifest.read_bytes()) if manifest.exists() else {}

    with (root / '.lock').open('a+b') as lock:
        if action == 'inventory':
            fcntl.flock(lock, fcntl.LOCK_SH)
            return reply(handler, 200, list(manifest_sizes(snapshot())))
        try:
            length = int(handler.headers['Content-Length'])
            if length < 0:
                raise ValueError('invalid Content-Length')
            if action == 'push':
                tree, keys, sizes = read_header(handler.rfile, length)
                with tempfile.TemporaryDirectory(dir=root) as name:
                    staging = Path(name)
                    for key in keys:
                        receive(handler.rfile, staging / key, key, sizes[key])
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if any(not (blobs / k).is_file() for k in sizes.keys() - set(keys)):
                        return reply(handler, 409, {'error': 'retry against the current workspace'})
                    for key in keys:
                        os.replace(staging / key, blobs / key)
                    staged_manifest = staging / 'manifest.json'
                    staged_manifest.write_text(json.dumps(tree, separators=(',', ':')))
                    os.replace(staged_manifest, manifest)
                    for blob in blobs.iterdir():
                        if blob.name not in sizes:
                            blob.unlink()
                return reply(handler, 200, {})
            known = json.loads(handler.rfile.read(length))
            if not isinstance(known, list) or not all(isinstance(k, str) for k in known):
                raise ValueError('invalid receiver inventory')
        except (IndexError, KeyError, TypeError, UnicodeError, ValueError, OverflowError) as exc:
            return reply(handler, 400, {'error': str(exc)})
        fcntl.flock(lock, fcntl.LOCK_SH)
        tree = snapshot()
        sizes = manifest_sizes(tree)
        send = sorted(sizes.keys() - set(known))
        prefix = header(tree, send)
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Content-Length', str(len(prefix) + sum(sizes[k] for k in send)))
        handler.end_headers()
        handler.wfile.write(prefix)
        for key in send:
            with (blobs / key).open('rb') as source:
                shutil.copyfileobj(source, handler.wfile, 1024 * 1024)
