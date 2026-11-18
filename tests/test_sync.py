import os
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_client as client
import sync_server


class SyncTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                sync_server.handle(self, self.headers['Authorization'].split(' ')[1])
            do_POST = do_GET

            def log_message(self, *args):
                pass

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = patch.dict(os.environ, MB_SYNC_DIR=str(root / 'store'))
        env.start()
        self.addCleanup(env.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(self.server.server_close)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        self.source = root / 'workspace'
        self.source.mkdir()
        self.cache = root / 'cache'
        self.args = (self.url, '1', 1, self.source, self.cache)

    def test_delta_in_both_directions_and_repeated_checkout(self):
        s = self.source
        (s / 'A').write_bytes(b'first')
        (s / 'B').write_bytes(b'first')
        (s / 'A').chmod(0o700)
        (s / 'link').symlink_to('../missing')
        (s / 'recursive').symlink_to(s)
        (s / 'empty').mkdir()
        with patch('sync_client.header', wraps=client.header) as upload, patch('sync_server.header', wraps=client.header) as download:
            client.push(*self.args)
            target = client.pull(*self.args)
            self.assertEqual(len(upload.call_args.args[1]), 1)
            self.assertEqual(len(download.call_args.args[1]), 1)
            self.assertEqual((target / 'A').stat().st_mode & 0o777, 0o700)
            self.assertEqual(os.readlink(target / 'recursive'), str(s))
            self.assertTrue((target / 'empty').is_dir())
            self.assertEqual(os.readlink(target / 'link'), '../missing')
            (s / 'A').rename(s / 'moved')
            (s / 'empty').rmdir()
            (s / 'B').chmod(0o751)
            client.push(*self.args)
            client.pull(*self.args)
            self.assertEqual(upload.call_args.args[1], [])
            self.assertEqual(download.call_args.args[1], [])
            self.assertEqual((target / 'moved').read_bytes(), b'first')
            self.assertFalse((target / 'A').exists())
            self.assertFalse((target / 'empty').exists())
            self.assertEqual((target / 'B').stat().st_mode & 0o777, 0o751)

    def test_failed_sync_preserves_previous_copy(self):
        source = self.source / 'A'
        source.write_text('one')
        client.push(*self.args)
        target = client.pull(*self.args)
        source.write_text('two')
        tree, files = client.scan(self.source)
        bad = client.header(tree, list(files)) + b'bad'
        with self.assertRaises(HTTPError) as failure:
            with client.request(self.url, '1', 1, 'push', bad):
                pass
        self.assertEqual(failure.exception.code, 400)
        failure.exception.close()
        client.pull(*self.args)
        self.assertEqual((target / 'A').read_text(), 'one')
        client.push(*self.args)
        with patch.object(client, 'receive', side_effect=OSError('no space')), self.assertRaises(OSError):
            client.pull(*self.args)
        self.assertEqual((target / 'A').read_text(), 'one')
        client.pull(*self.args)
        self.assertEqual((target / 'A').read_text(), 'two')

    def test_pull_keeps_one_snapshot_during_concurrent_push(self):
        source = self.source / 'A'
        source.write_text('before')
        client.push(*self.args)
        entered, resume = Event(), Event()
        original = sync_server.shutil.copyfileobj

        def delayed(*args):
            entered.set()
            if not resume.wait(10):
                raise RuntimeError('reader did not resume')
            return original(*args)

        with patch.object(sync_server.shutil, 'copyfileobj', delayed), ThreadPoolExecutor(2) as pool:
            reading = pool.submit(client.pull, *self.args)
            self.assertTrue(entered.wait(5))
            source.write_text('after')
            writing = pool.submit(client.push, *self.args)
            resume.set()
            target = reading.result(timeout=10)
            writing.result(timeout=10)
        self.assertEqual((target / 'A').read_text(), 'before')
        client.pull(*self.args)
        self.assertEqual((target / 'A').read_text(), 'after')

    def test_wrong_owner_invalid_paths_and_overlap(self):
        with self.assertRaises(HTTPError) as failure:
            with client.request(self.url, '2', 1, 'inventory'):
                pass
        self.assertEqual(failure.exception.code, 403)
        failure.exception.close()
        for tree in [{'../x': ['d', 511]}, {'a': ['l', '/tmp'], 'a/b': ['d', 511]}]:
            with self.assertRaises(HTTPError) as failure:
                with client.request(self.url, '1', 1, 'push', client.header(tree, [])):
                    pass
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()
        self.cache.symlink_to(self.source)
        with self.assertRaises(ValueError):
            client.push(*self.args)


if __name__ == '__main__':
    unittest.main()
