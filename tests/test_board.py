"""Black-box tests against the packaged service and the real mb client."""
import concurrent.futures
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CLIENT = Path(__file__).resolve().parents[1] / "mb"
ARCH = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine(), platform.machine())
IMAGE = os.environ.get("MB_TEST_IMAGE", "mb-board:0.1-" + ARCH)


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE).strip()


def wait_ready(url):
    for _ in range(150):
        try:
            with urlopen(url + "/rank", timeout=1):
                return
        except HTTPError as exc:
            if exc.code == 401:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("mb service did not become ready")


def payload(score):
    return {"plans": ["Plan-A"], "scenario": "test", "change": "candidate A",
            "result": {"score": score, "summary": "review passed"}}


class BoardIntegration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.accounts = {str(i): secrets.token_urlsafe(18) for i in range(1, 14)}
        accounts = self.base / "accounts.json"
        accounts.write_text(json.dumps(self.accounts))
        accounts.chmod(0o600)
        # Exercise a leading '-' on both first boot and restart against real PocketBase.
        if self._testMethodName == "test_workspace_and_restart":
            service = ["--entrypoint", "python3", IMAGE, "-c",
                       "import secrets, server; "
                       "secrets.token_urlsafe = lambda n: '-' + secrets.token_hex(n); server.main()"]
        else:
            service = [IMAGE]
        self.container = docker("run", "-d", "-p", "127.0.0.1::8090", "-v", "/data",
                                "-v", f"{accounts}:/accounts.json:ro",
                                "-e", "MB_ACCOUNTS_FILE=/accounts.json",
                                "-e", "MB_DIRECTION=" + ("min" if self._testMethodName.endswith("_min") else "max"),
                                *service)
        self.addCleanup(docker, "rm", "-fv", self.container)
        self.connect()
        for rank in self.accounts:
            work = self.base / rank / "workspace"
            work.mkdir(parents=True)
            (work / "rank.txt").write_text(rank)

    def connect(self):
        self.url = "http://" + docker("port", self.container, "8090/tcp").splitlines()[0]
        wait_ready(self.url)

    def env(self, rank):
        base = self.base / str(rank)
        return dict(os.environ, MB_URL=self.url, MB_RANK=str(rank),
                    MB_PASSWORD=self.accounts[str(rank)], MB_WORKSPACE=str(base / "workspace"),
                    MB_CACHE_DIR=str(base / "cache"), PYTHONDONTWRITEBYTECODE="1")

    def mb(self, rank, *args, stdin=None, env=None, ok=True):
        proc = subprocess.run([sys.executable, str(CLIENT), *args], text=True,
                              input=stdin, capture_output=True, timeout=90,
                              env={**self.env(rank), **(env or {})})
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def publish(self, rank, rnd, score):
        return self.mb(rank, "publish", rnd, "--json", json.dumps(payload(score)))

    def test_publication_lifecycle(self):
        self.assertEqual(self.mb(1, "rank")["items"], [])
        self.assertEqual(self.publish(1, "r1", 90), {"rank": 1, "round": "r1"})
        record = payload(110)
        record["paths"] = ["rank.txt"]
        self.mb(1, "publish", "r2", "--json", "-", stdin=json.dumps(record))
        self.mb(1, "publish", "r2", "--json", json.dumps(record))
        self.mb(1, "publish", "r2", "--json", json.dumps(payload(99)), ok=False)
        self.publish(2, "r1", 100)
        page = self.mb(2, "rank", "--limit", "1")
        self.assertEqual(page["items"], [{"position": 1, "rank": 1, "score": 110, "round": "r2"}])
        tail = self.mb(2, "rank", "--limit", "1", "--cursor", page["next_cursor"])
        self.assertEqual(tail["items"][0]["rank"], 2)
        self.assertFalse(tail.get("next_cursor"))
        page = self.mb(2, "posts", "1", "--limit", "1")
        self.assertEqual(page["items"][0]["round"], "r2")
        self.assertEqual(self.mb(2, "posts", "1", "--limit", "1", "--cursor", page["next_cursor"])
                         ["items"][0]["round"], "r1")
        self.assertEqual(self.mb(2, "posts", "1", "r2"), {"rank": 1, "round": "r2", **record})
        self.mb(1, "retract", "r2")
        self.assertEqual([r["score"] for r in self.mb(2, "rank")["items"]], [100, 90])
        self.mb(1, "retract", "r1")
        self.assertEqual([r["rank"] for r in self.mb(2, "rank")["items"]], [2])
        self.mb(1, "posts", "1", "r2", ok=False)
        self.publish(1, "r2", 95)
        self.assertEqual(self.mb(2, "posts", "1", "r2")["result"]["score"], 95)

    def test_workspace_and_restart(self):
        w = self.base / "1/workspace"
        (w / "dir").mkdir()
        (w / "dir/script").write_text("one")
        (w / "dir/script").chmod(0o751)
        (w / "alias").symlink_to("dir/script")
        (w / "dangling").symlink_to("../outside")
        (w / "empty").mkdir()
        (w / "binary").write_bytes(bytes(range(256)) * 8192)
        self.publish(1, "r1", 10)
        self.mb(2, "sync", "pull", "1")
        cache = self.base / "2/cache/1"
        self.assertEqual((cache / "dir/script").read_text(), "one")
        self.assertEqual((cache / "dir/script").stat().st_mode & 0o777, 0o751)
        self.assertEqual(os.readlink(cache / "dangling"), "../outside")
        self.assertEqual(os.readlink(cache / "alias"), "dir/script")
        self.assertTrue((cache / "empty").is_dir())
        self.assertEqual((cache / "binary").read_bytes(), (w / "binary").read_bytes())
        (w / "alias").unlink()
        (w / "dir/script").write_text("two")
        self.mb(1, "sync", "push")
        self.assertEqual(self.mb(2, "posts", "1", "r1")["result"]["score"], 10)
        docker("restart", self.container)
        self.connect()
        self.mb(2, "sync", "pull", "1")
        self.assertFalse((cache / "alias").exists())
        self.assertEqual((cache / "dir/script").read_text(), "two")
        self.mb(1, "retract", "r1")
        self.mb(2, "sync", "pull", "1")
        self.assertEqual(self.mb(1, "rank")["items"], [])

    def test_failed_publish_and_auth(self):
        self.mb(1, "publish", "bad", "--json", "{}", ok=False)
        self.mb(1, "publish", "missing", "--json", json.dumps(payload(4)),
                env={"MB_WORKSPACE": str(self.base / "missing")}, ok=False)
        work = self.base / "1/workspace"
        link = self.base / "link"
        link.symlink_to(work, target_is_directory=True)
        self.mb(1, "publish", "nested", "--json", json.dumps(payload(4)),
                env={"MB_CACHE_DIR": str(link / "cache")}, ok=False)
        self.mb(1, "rank", env={"MB_PASSWORD": self.accounts["2"]}, ok=False)
        self.assertEqual(self.mb(1, "rank")["items"], [])
        with self.assertRaises(HTTPError) as err:
            urlopen(Request(self.url + "/posts/1", method="GET"), timeout=5)
        self.assertEqual(err.exception.code, 401)
        self.publish(2, "other", 3)
        self.mb(1, "retract", "other")
        self.assertEqual(self.mb(1, "posts", "2", "other")["result"]["score"], 3)

    def test_parallel_publications_and_full_pull(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda r: self.publish(r, "r1", r), range(1, 13)))
        page = self.mb(13, "rank")
        self.assertEqual(len(page["items"]), 10)
        self.assertEqual([r["rank"] for r in page["items"]], list(range(12, 2, -1)))
        self.mb(13, "sync", "pull")
        dest = self.base / "13/cache"
        for r in range(1, 13):
            self.assertEqual((dest / str(r) / "rank.txt").read_text(), str(r))
        # A reader cannot rescan rank 11's modified cache. Later ranks still update.
        os.mkfifo(dest / "11" / "local.pipe")
        (self.base / "1/workspace/rank.txt").write_text("updated")
        self.mb(1, "sync", "push")
        partial = self.mb(13, "sync", "pull", ok=False)["items"]
        self.assertEqual([r["rank"] for r in partial if "error" in r], [11])
        self.assertEqual((dest / "1/rank.txt").read_text(), "updated")

    def test_direction_min(self):
        self.publish(1, "r1", 5)
        self.publish(1, "r2", 15)
        self.publish(2, "r1", 10)
        self.assertEqual([r["score"] for r in self.mb(1, "rank")["items"]], [5, 10])

    def test_concurrent_retry_of_one_publication(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.publish(1, "r1", 0), range(4)))
        self.assertTrue(all(item == {"rank": 1, "round": "r1"} for item in results))
        self.assertEqual(len(self.mb(2, "posts", "1")["items"]), 1)
        self.assertEqual(self.mb(2, "rank")["items"][0]["score"], 0)

    def test_isolated_experiment_containers(self):
        network = "mb-test-" + self.container[:12]
        docker("network", "create", network)
        self.addCleanup(docker, "network", "rm", network)
        docker("network", "connect", "--alias", "board", network, self.container)
        self.addCleanup(docker, "network", "disconnect", network, self.container)

        def client(rank, *args):
            root = self.base / str(rank)
            (root / "cache").mkdir(exist_ok=True)
            raw = docker("run", "--rm", "--network", network, "--user", f"{os.getuid()}:{os.getgid()}",
                         "-e", "MB_URL=http://board:8090", "-e", f"MB_RANK={rank}",
                         "-e", "MB_PASSWORD=" + self.accounts[str(rank)],
                         "-v", f"{root}/workspace:/workspace:ro",
                         "-v", f"{root}/cache:/board-cache", "--entrypoint", "/opt/mb/mb",
                         IMAGE, *args)
            return json.loads(raw) if raw else None

        self.assertEqual(client(1, "publish", "r1", "--json", json.dumps(payload(12))),
                         {"rank": 1, "round": "r1"})
        self.assertEqual(client(2, "rank")["items"][0]["rank"], 1)
        client(2, "sync", "pull", "1")
        self.assertEqual((self.base / "2/cache/1/rank.txt").read_text(), "1")
        self.assertEqual((self.base / "2/workspace/rank.txt").read_text(), "2")


if __name__ == "__main__":
    unittest.main()
