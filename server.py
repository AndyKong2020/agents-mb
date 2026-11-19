"""Experiment board gateway backed by a loopback PocketBase process."""

import hashlib
import json
import os
import secrets
import signal
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from payload import validate_payload
import sync_server


class Failure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class PocketBase:
    def __init__(self, port):
        self.url = f'http://127.0.0.1:{port}/api/'
        self.opener = build_opener(ProxyHandler({}))

    def api(self, method, path, body=None, *, token='', **query):
        url = self.url + path + ('?' + urlencode(query) if query else '')
        headers = {'Content-Type': 'application/json', 'Authorization': token}
        req = Request(url, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(req, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else None
        except HTTPError as exc:
            detail = json.loads(exc.read())
            raise Failure(exc.code, detail.get('message', exc.reason)) from exc

    def login(self, collection, identity, password):
        return self.api('POST', f'collections/{collection}/auth-with-password',
                        {'identity': identity, 'password': password})['token']


# PocketBase accepts email identities and enforces a minimum password length.
# Keep the experiment credential format independent of these storage conventions.
def credentials(rank, password):
    if not isinstance(password, str):
        raise ValueError('password must be a string')
    return f'r{rank}@mb.invalid', hashlib.sha256(password.encode()).hexdigest()


def provision(pb, accounts, admin):
    path = 'collections/experiments/records'
    for rank, password in accounts.items():
        email, secret = credentials(int(rank), password)
        records = pb.api('GET', path, token=admin, filter=f'rank = {int(rank)}')['items']
        data = {'password': secret, 'passwordConfirm': secret}
        if not records:
            pb.api('POST', path, dict(data, email=email, rank=int(rank)), token=admin)
        else:
            try:
                pb.login('experiments', email, secret)
            except Failure as exc:
                if exc.status != 400:
                    raise
                pb.api('PATCH', path + '/' + records[0]['id'], data, token=admin)


POSTS = 'collections/publications/records'


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        sync_server.reply(self, status, data)

    def read_json(self):
        size = int(self.headers.get('Content-Length', '0'))
        if size <= 0:
            raise ValueError('expected a JSON body')
        return json.loads(self.rfile.read(size))

    def dispatch(self):
        try:
            result = self.route()
            if result is not None:
                self.reply(200, result)
        except Failure as exc:
            self.reply(exc.status, {'message': str(exc)})
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            self.reply(400, {'message': str(exc)})
        except (URLError, TimeoutError, ConnectionError):
            self.reply(503, {'message': 'board storage unavailable'})

    do_GET = do_POST = do_DELETE = dispatch

    def route(self):
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split('/')[1:]]
        pb = self.server.pb
        if self.command == 'POST' and parts == ['login']:
            body = self.read_json()
            rank = int(body['rank'])
            if rank not in self.server.accounts:
                raise Failure(401, 'invalid experiment credentials')
            ident, password = credentials(rank, body['password'])
            try:
                return {'token': pb.login('experiments', ident, password)}
            except Failure as exc:
                if exc.status != 400:
                    raise
                raise Failure(401, 'invalid experiment credentials') from exc
        scheme, _, token = self.headers.get('Authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not token:
            raise Failure(401, 'experiment authentication required')
        record = pb.api('POST', 'collections/experiments/auth-refresh', token=token)['record']
        rank = int(record['rank'])
        if rank not in self.server.accounts:
            raise Failure(401, 'experiment authentication required')
        if parts[0] == 'sync':
            sync_server.handle(self, rank)
            return None
        if self.command == 'GET':
            if parts == ['rank']:
                return self.listing(token, parse_qs(url.query), None)
            if parts[0] == 'posts' and len(parts) == 2:
                return self.listing(token, parse_qs(url.query), int(parts[1]))
            if parts[0] == 'posts' and len(parts) == 3:
                item = self.find_post(token, int(parts[1]), parts[2])
                if item:
                    return dict(item['payload'], rank=item['rank'], round=item['round'])
                raise Failure(404, 'publication not found')
        elif parts[0] == 'posts' and len(parts) == 2 and parts[1]:
            rnd = parts[1]
            if self.command == 'DELETE':
                item = self.find_post(token, rank, rnd)
                if item:
                    try:
                        pb.api('DELETE', POSTS + '/' + item['id'], token=token)
                    except Failure as exc:
                        if exc.status != 404:
                            raise
                return {}
            payload = self.read_json()
            validate_payload(payload)
            try:
                pb.api('POST', POSTS, {'rank': rank, 'round': rnd, 'payload': payload,
                                       'score': payload['result']['score']}, token=token)
            except Failure as exc:
                if exc.status != 400:
                    raise
                item = self.find_post(token, rank, rnd)
                if not item:
                    raise
                if item['payload'] != payload:
                    raise Failure(409, 'different publication already exists') from exc
            return {'rank': rank, 'round': rnd}
        raise Failure(404, 'unknown endpoint')

    def find_post(self, token, rank, rnd):
        condition = f'rank = {rank} && round = {json.dumps(rnd, ensure_ascii=False)}'
        items = self.server.pb.api('GET', POSTS, token=token, filter=condition, perPage=1)['items']
        return items[0] if items else None

    def listing(self, token, query, rank):
        limit = int(query.get('limit', ['10'])[0])
        offset = int(query.get('cursor', ['0'])[0])
        if limit < 1 or offset < 0:
            raise ValueError('invalid pagination')
        direction = self.server.direction
        if rank is None:
            path = 'collections/leaders_' + direction + '/records'
            params = {'sort': ('-' if direction == 'max' else '') + 'score,rank'}
        else:
            path = POSTS
            params = {'sort': '-created,-id', 'filter': f'rank = {rank}'}
        # PocketBase accepts pages of at most 1000 records. The external cursor
        # counts consumed rows, so callers can change the requested page size.
        page_size = min(limit, 1000)
        page, skip = divmod(offset, page_size)
        items = []
        while True:
            batch = self.server.pb.api('GET', path, token=token, page=page + 1,
                                       perPage=page_size, **params)
            items.extend(batch['items'][skip:skip + limit - len(items)])
            if len(items) == limit or not batch['items'] or page + 1 >= batch['totalPages']:
                break
            page += 1
            skip = 0
        if rank is None:
            rows = [dict(position=i, rank=item['rank'], score=item['score'], round=item['round'])
                    for i, item in enumerate(items, offset + 1)]
        else:
            rows = [dict(round=item['round'], plans=item['payload']['plans'], score=item['score'],
                         summary=item['payload']['result']['summary']) for item in items]
        response = {'items': rows}
        if rows and offset + len(rows) < batch['totalItems']:
            response['next_cursor'] = str(offset + len(rows))
        return response


def main():
    data = Path(os.getenv('MB_DATA', '/data'))
    os.environ.setdefault('MB_SYNC_DIR', str(data / 'workspaces'))
    direction = os.getenv('MB_DIRECTION', 'max')
    if direction not in ('min', 'max'):
        raise ValueError('MB_DIRECTION must be min or max')
    accounts = json.loads(Path(os.environ['MB_ACCOUNTS_FILE']).read_bytes())
    addr = int(os.getenv('MB_PB_PORT', '8091'))
    pb = PocketBase(addr)
    cmd = [os.getenv('MB_POCKETBASE', 'pocketbase'), '--dir', str(data / 'pb_data'),
           '--migrationsDir', str(Path(__file__).with_name('pb_migrations'))]
    admin_id = 'bootstrap@mb.invalid'
    secret = secrets.token_urlsafe(32)
    setup = subprocess.run(cmd + ['superuser', 'upsert', '--', admin_id, secret],
                           stdout=subprocess.DEVNULL)
    if setup.returncode:
        raise RuntimeError('PocketBase bootstrap failed')
    with subprocess.Popen(cmd + ['serve', '--http', f'127.0.0.1:{addr}']) as child:
        def stop(signum, frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        try:
            for _ in range(150):
                if child.poll() is not None:
                    raise RuntimeError('PocketBase stopped during startup')
                try:
                    pb.api('GET', 'health')
                    break
                except URLError:
                    time.sleep(0.2)
            admin = pb.login('_superusers', admin_id, secret)
            provision(pb, accounts, admin)
            with ThreadingHTTPServer(('', int(os.getenv('MB_PORT', '8090'))), Handler) as board:
                board.pb, board.direction = pb, direction
                board.accounts = {int(rank) for rank in accounts}
                board.serve_forever()
        finally:
            child.terminate()


if __name__ == '__main__':
    main()
