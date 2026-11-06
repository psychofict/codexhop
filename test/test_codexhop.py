"""Tests for codexhop. Standard library only: python3 -m unittest discover -s test

Every test runs the real command in a subprocess against a throwaway HOME and
CODEX_HOME. The logins are made-up JWTs (unsigned, which codexhop never
checks), the token and usage endpoints are a local fake server, and `codex` is a
small script that records how it was called. Nothing leaves the machine.

TEST_PYTHON picks the interpreter that runs codexhop (default: this one), so
the same suite checks the oldest supported Python: TEST_PYTHON=python3.9.
"""

from __future__ import annotations

import base64
import http.server
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "codexhop.py")
PYTHON = shutil.which(os.environ.get("TEST_PYTHON") or "") or sys.executable
AUTH = "https://api.openai.com/auth"


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def jwt(claims: dict) -> str:
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


_SERIAL = [0]


def login(email="a@example.com", user="user-a", account="ws-a", plan="plus",
          iat=None, life=240 * 3600, last_refresh=None, field=None) -> dict:
    """An auth.json as Codex writes it, for a made-up ChatGPT login."""
    _SERIAL[0] += 1
    iat = int(time.time()) - 60 if iat is None else int(iat)
    claims = {AUTH: {"chatgpt_user_id": user, "user_id": user, "chatgpt_account_id": account,
                     "chatgpt_plan_type": plan}}
    tag = f"{user}-{account}-{_SERIAL[0]}"
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": jwt({"email": email, "iat": iat, "exp": iat + 3600, **claims}),
            "access_token": jwt({"iat": iat, "exp": iat + life, "at": tag, **claims,
                                 "https://api.openai.com/profile": {"email": email}}),
            "refresh_token": f"rt-{tag}",
            "account_id": field or account,
        },
        "last_refresh": last_refresh or time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(iat)),
    }


A = dict(email="ann@example.com", user="user-a", account="ws-a", plan="plus")
B = dict(email="bob@example.com", user="user-b", account="ws-b", plan="pro")
C = dict(email="cat@example.com", user="user-c", account="ws-c", plan="team")


def secrets_in(doc: dict) -> list[str]:
    t = doc.get("tokens") or {}
    return [v for v in (t.get("id_token"), t.get("access_token"), t.get("refresh_token"),
                        doc.get("OPENAI_API_KEY")) if v]


# ------------------------------------------------------------------ fake server


class FakeServer:
    """The token endpoint and the usage endpoint, on 127.0.0.1."""

    def __init__(self):
        self.refresh: dict = {}  # refresh token -> new auth.json, or (status, body)
        self.usage: dict = {}  # access token -> (status, body, headers)
        self.calls: list = []
        server = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body, headers=None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}
                server.calls.append(("POST", self.path, headers, body))
                got = server.refresh.get(body.get("refresh_token"))
                if isinstance(got, dict):
                    t = got["tokens"]
                    self.reply(200, {"id_token": t["id_token"], "access_token": t["access_token"],
                                     "refresh_token": t["refresh_token"]})
                elif isinstance(got, tuple):
                    self.reply(*got)
                else:
                    self.reply(401, {"error": {"code": "refresh_token_invalidated", "message": "nope"}})

            def do_GET(self):
                server.calls.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None))
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                got = server.usage.get(tok)
                if got is None:
                    self.reply(401, {"detail": "unknown token"})
                else:
                    self.reply(*got)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def usage_body(short=10.0, long=20.0, plan="plus", reset_in=3600, reached=False):
    now = int(time.time())
    return {
        "plan_type": plan,
        "rate_limit": {
            "allowed": True, "limit_reached": reached,
            "primary_window": {"used_percent": short, "limit_window_seconds": 18000,
                               "reset_after_seconds": reset_in, "reset_at": now + reset_in},
            "secondary_window": {"used_percent": long, "limit_window_seconds": 604800,
                                 "reset_after_seconds": 86400, "reset_at": now + 86400},
        },
        "additional_rate_limits": [],
        "credits": {"has_credits": False, "unlimited": False, "balance": None},
    }


FAKE_CODEX = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for `codex` that records how it was called."""
    import json, os, sys
    log = os.environ.get("FAKE_CODEX_LOG")
    home = os.environ.get("CODEX_HOME", "")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "home": home}) + "\\n")
    if sys.argv[1:2] == ["--version"]:
        print("codex-cli " + os.environ.get("FAKE_CODEX_VERSION", "0.166.0"))
        sys.exit(0)
    if sys.argv[1:2] == ["login"]:
        cfg = open(os.path.join(home, "config.toml")).read()
        assert 'cli_auth_credentials_store = "file"' in cfg, cfg
        src = os.environ.get("FAKE_CODEX_LOGIN")
        if src:
            with open(src) as f, open(os.path.join(home, "auth.json"), "w") as g:
                g.write(f.read())
        sys.exit(0)
    rotate = os.environ.get("FAKE_CODEX_ROTATE")
    if rotate:
        with open(rotate) as f, open(os.path.join(home, "auth.json"), "w") as g:
            g.write(f.read())
    sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
''')


# ------------------------------------------------------------------- the tests


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.refresh.clear()
        self.server.usage.clear()
        self.server.calls.clear()
        self.tmp = tempfile.mkdtemp(prefix="codexhop-test-")
        self.home = os.path.join(self.tmp, "home")
        self.codex_home = os.path.join(self.home, ".codex")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.codex_home)
        os.makedirs(self.bin)
        fake = os.path.join(self.bin, "codex")
        with open(fake, "w") as f:
            f.write(FAKE_CODEX.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        os.chmod(fake, 0o755)
        self.log = os.path.join(self.tmp, "codex.log")
        self.store = os.path.join(self.codex_home, "codexhop")
        self.children: list = []
        self.env = {
            "HOME": self.home,
            "PATH": self.bin + os.pathsep + "/usr/bin:/bin",
            "CODEX_HOME": self.codex_home,
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE": self.server.url("/oauth/token"),
            "CODEXHOP_USAGE_URL": self.server.url("/backend-api/wham/usage"),
            "CODEXHOP_LOCK_WAIT": "1",
            "FAKE_CODEX_LOG": self.log,
            "NO_COLOR": "1",
            "LANG": "C.UTF-8",
        }

    def tearDown(self):
        for p in self.children:
            p.kill()
            p.wait()
            if p.stdout:
                p.stdout.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers

    def run_tool(self, *args, code=0, input=None, env=None):
        p = subprocess.run([PYTHON, TOOL, *args], capture_output=True, text=True, input=input,
                           env={**self.env, **(env or {})}, timeout=60)
        if code is not None:
            self.assertEqual(p.returncode, code, f"{args}\nstdout: {p.stdout}\nstderr: {p.stderr}")
        return p

    def write(self, path: str, doc: dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(doc, f)
        os.chmod(path, 0o600)

    def read(self, path: str) -> dict:
        with open(path) as f:
            return json.load(f)

    def text(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def set_live(self, doc: dict):
        self.write(os.path.join(self.codex_home, "auth.json"), doc)

    def live(self) -> dict:
        return self.read(os.path.join(self.codex_home, "auth.json"))

    def acct(self, name: str) -> str:
        return os.path.join(self.store, "accounts", f"{name}.json")

    def save(self, name: str, doc: dict):
        self.write(self.acct(name), doc)
        os.chmod(os.path.join(self.store, "accounts"), 0o700)
        os.chmod(self.store, 0o700)

    def codex_calls(self) -> list:
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    def fake_running_codex(self, home: str | None = None) -> int:
        """A process that looks like `codex` (argv[0] ends in /codex) on this Codex home."""
        d = os.path.join(self.tmp, "procs", str(len(self.children)))
        os.makedirs(d)
        os.symlink(shutil.which("sleep"), os.path.join(d, "codex"))
        p = subprocess.Popen([os.path.join(d, "codex"), "60"], env={"CODEX_HOME": home or self.codex_home})
        self.children.append(p)
        time.sleep(0.1)
        return p.pid


class Identity(unittest.TestCase):
    """The parts that need no files: reading who a login is."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("codexhop_mod", TOOL)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_claims_name_the_login(self):
        ident = self.m.identity(login(**A))
        self.assertEqual(ident["email"], "ann@example.com")
        self.assertEqual(ident["plan"], "plus")
        self.assertEqual(ident["key"], "chatgpt:user-a:ws-a")
        self.assertFalse(self.m.mixed(ident))

    def test_same_person_other_workspace_is_another_login(self):
        a1 = self.m.identity(login(**A))
        a2 = self.m.identity(login(**{**A, "account": "ws-z"}))
        self.assertNotEqual(a1["key"], a2["key"])

    def test_account_field_is_not_trusted(self):
        doc = login(**{**A, "field": "ws-b"})
        ident = self.m.identity(doc)
        self.assertTrue(self.m.mixed(ident))
        self.assertEqual(ident["key"], "chatgpt:user-a:ws-a")
        self.assertEqual(self.m.normalised(doc)["tokens"]["account_id"], "ws-a")

    def test_api_key_login(self):
        ident = self.m.identity({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test-1234"})
        self.assertEqual(ident["kind"], "apikey")
        self.assertTrue(ident["key"].startswith("apikey:"))
        self.assertNotIn("sk-test", json.dumps(ident))

    def test_newer_copy_by_issue_time(self):
        old, new = login(**A, iat=time.time() - 9000), login(**A)
        self.assertTrue(self.m.newer(new, old))
        self.assertFalse(self.m.newer(old, new))

    def test_codex_process_detection(self):
        self.assertTrue(self.m.is_codex(["/home/u/.local/bin/codex", "resume"]))
        self.assertTrue(self.m.is_codex(["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"]))
        self.assertTrue(self.m.is_codex(["/usr/bin/node", "/usr/bin/codex"]))
        self.assertFalse(self.m.is_codex(["node", "-e", "require('/home/u/.codex/plugins/x')"]))
        self.assertFalse(self.m.is_codex(["python3", "/usr/bin/codexhop"]))

    def test_usage_windows_go_by_length(self):
        body = usage_body(short=42, long=7)
        got = self.m.parse_usage(body, time.time())
        self.assertEqual(got["short"]["pct"], 42.0)
        self.assertEqual(got["long"]["pct"], 7.0)
        weekly_only = {"rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": 604800,
                                                         "reset_at": int(time.time()) + 99}}}
        got = self.m.parse_usage(weekly_only, time.time())
        self.assertIsNone(got["short"])
        self.assertEqual(got["long"]["pct"], 5.0)

    def test_window_past_its_reset_is_empty(self):
        win = {"pct": 80.0, "seconds": 18000, "resetsAt": time.time() - 5}
        now = self.m.window_now(win)
        self.assertEqual(now["pct"], 0.0)
        self.assertEqual(now["was"], 80.0)

    def test_config_value(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('model = "x"\ncli_auth_credentials_store = "keyring" # here\n[profiles.p]\n'
                    'cli_auth_credentials_store = "file"\n')
        try:
            self.assertEqual(self.m.config_value("cli_auth_credentials_store", f.name), "keyring")
            self.assertIsNone(self.m.config_value("chatgpt_base_url", f.name))
        finally:
            os.remove(f.name)


class SaveListWhoami(Base):
    def test_save_names_the_login_after_its_email(self):
        self.set_live(login(**A))
        p = self.run_tool("save")
        self.assertIn("saved ann", p.stdout)
        self.assertEqual(self.read(self.acct("ann")), self.live())
        self.assertEqual(stat.S_IMODE(os.stat(self.acct("ann")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o700)

    def test_save_refuses_a_second_name_for_one_login(self):
        self.set_live(login(**A))
        self.run_tool("save", "work")
        p = self.run_tool("save", "other", code=1)
        self.assertIn("already saved as 'work'", p.stderr)

    def test_list_and_whoami_need_no_network(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        p = self.run_tool("list", env={"CODEXHOP_OFFLINE": "1"})
        lines = p.stdout.splitlines()
        self.assertTrue(any(ln.startswith("*") and "ann" in ln and "ann@example.com" in ln for ln in lines))
        self.assertTrue(any("bob@example.com" in ln and "pro" in ln for ln in lines))
        p = self.run_tool("whoami")
        self.assertIn("ann@example.com", p.stdout)
        self.assertIn("saved as: ann", p.stdout)
        self.assertEqual(self.run_tool("active").stdout.strip(), "ann")
        self.assertEqual(self.server.calls, [])

    def test_json_never_carries_a_token(self):
        live = login(**A)
        self.set_live(live)
        self.save("ann", live)
        bob = login(**B)
        self.save("bob", bob)
        out = ""
        for args in (("list", "--json"), ("whoami", "--json"), ("doctor", "--json"), ("active", "--json")):
            out += self.run_tool(*args, code=None).stdout
        json.loads(self.run_tool("list", "--json").stdout)
        for secret in secrets_in(live) + secrets_in(bob):
            self.assertNotIn(secret, out)
        self.assertIn("ann@example.com", out)

    def test_one_email_in_two_workspaces_shows_the_workspace(self):
        self.save("home", login(**A))
        self.save("team", login(**{**A, "account": "ws-team-1234"}))
        p = self.run_tool("list")
        self.assertIn("WORKSPACE", p.stdout)
        self.assertIn("ws-team-", p.stdout)

    def test_bare_command_without_a_terminal_lists(self):
        self.save("ann", login(**A))
        self.save("bob", login(**B))
        p = self.run_tool()
        self.assertIn("NAME", p.stdout)
        self.assertIn("bob", p.stdout)


class Hop(Base):
    def test_hop_swaps_the_login_and_files_the_old_one(self):
        old_a = login(**A, iat=time.time() - 7200)
        self.save("ann", old_a)
        renewed_a = login(**A)  # Codex renewed the live copy since the last save
        self.set_live(renewed_a)
        bob = login(**B)
        self.save("bob", bob)
        p = self.run_tool("bob")
        self.assertIn("switched to bob", p.stdout)
        self.assertEqual(self.live(), bob)
        self.assertEqual(self.read(self.acct("ann")), renewed_a)
        self.assertEqual(self.text(os.path.join(self.store, "active")).strip(), "bob")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.codex_home, "auth.json")).st_mode), 0o600)

    def test_an_older_live_copy_never_overwrites_a_newer_saved_one(self):
        newer_a = login(**A)
        self.save("ann", newer_a)
        self.set_live(login(**A, iat=time.time() - 7200))
        self.save("bob", login(**B))
        self.run_tool("use", "bob")
        self.assertEqual(self.read(self.acct("ann")), newer_a)

    def test_an_unknown_live_login_is_stashed_not_dropped(self):
        cat = login(**C)
        self.set_live(cat)
        self.save("bob", login(**B))
        p = self.run_tool("bob")
        self.assertIn("as 'cat'", p.stderr)
        self.assertEqual(self.read(self.acct("cat")), cat)

    def test_a_mixed_file_is_filed_by_its_tokens(self):
        # What Codex before 0.164 could leave: ann's renewed tokens under bob's account_id.
        mixed = login(**{**A, "field": "ws-b"})
        self.set_live(mixed)
        self.save("ann", login(**A, iat=time.time() - 7200))
        bob = login(**B)
        self.save("bob", bob)
        self.run_tool("bob")
        filed = self.read(self.acct("ann"))
        self.assertEqual(filed["tokens"]["access_token"], mixed["tokens"]["access_token"])
        self.assertEqual(filed["tokens"]["account_id"], "ws-a")
        self.assertEqual(self.read(self.acct("bob")), bob)

    def test_already_on_it(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        p = self.run_tool("ann")
        self.assertIn("already on ann", p.stdout)

    def test_an_expired_target_is_renewed_first_and_saved_before_the_hop(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        stale = login(**B, iat=time.time() - 300 * 3600)
        self.save("bob", stale)
        fresh = login(**B)
        self.server.refresh[stale["tokens"]["refresh_token"]] = fresh
        self.run_tool("bob")
        saved = self.read(self.acct("bob"))
        self.assertEqual(saved["tokens"]["refresh_token"], fresh["tokens"]["refresh_token"])
        self.assertEqual(self.live()["tokens"]["access_token"], fresh["tokens"]["access_token"])
        call = self.server.calls[0]
        self.assertEqual(call[3]["grant_type"], "refresh_token")
        self.assertEqual(call[3]["client_id"], "app_EMoamEEZ73f0CkXaXp7hrann")

    def test_a_target_open_in_a_run_session_needs_yes(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        pid = self.fake_running_codex(os.path.join(self.store, "run", "bob"))
        pids = os.path.join(self.store, "run", "bob", ".codexhop-pids")
        os.makedirs(pids)
        open(os.path.join(pids, str(pid)), "w").close()
        p = self.run_tool("bob", code=1)
        self.assertIn("open in a `codexhop run` session", p.stderr)
        self.run_tool("bob", "--yes")

    def test_open_sessions_are_told_what_happens(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        self.fake_running_codex()
        p = self.run_tool("bob")
        self.assertIn("codex session(s) open", p.stderr)
        self.assertIn("'ann'", p.stderr)

    def test_no_hop_while_an_open_session_is_about_to_renew(self):
        due = login(**A, life=200)  # inside Codex's 5-minute renewal window
        self.set_live(due)
        self.save("ann", due)
        self.save("bob", login(**B))
        self.fake_running_codex()
        p = self.run_tool("bob", code=1)
        self.assertIn("due for renewal", p.stderr)
        self.assertEqual(self.live(), due)
        self.run_tool("bob", "--yes")
        self.assertEqual(self.live()["tokens"]["account_id"], "ws-b")

    def test_an_old_renewal_with_time_left_does_not_stop_a_hop(self):
        live = login(**A, iat=time.time() - 8.5 * 86400)  # 36 hours of access token left
        self.set_live(live)
        self.save("ann", live)
        self.save("bob", login(**B))
        self.fake_running_codex()
        p = self.run_tool("bob")
        self.assertIn("in about 1d", p.stderr)

    def test_the_note_says_when_open_sessions_stop(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        self.save("zoe", login(email="zoe@example.com", user="user-z", account="ws-a"))
        self.fake_running_codex()
        p = self.run_tool("bob")
        self.assertIn("in about 9d", p.stderr)
        self.assertIn("signed in to another account", p.stderr)
        self.run_tool("ann")
        p = self.run_tool("zoe")
        self.assertIn("same workspace", p.stderr)

    def test_old_codex_with_open_sessions_gets_a_note(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        self.fake_running_codex()
        p = self.run_tool("bob", env={"FAKE_CODEX_VERSION": "0.160.1"})
        self.assertIn("Codex 0.160.1", p.stderr)

    def test_hop_waits_for_codex_auth_lock_then_gives_up(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, os, time
            fd = os.open({os.path.join(self.codex_home, 'auth.lock')!r}, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("held", flush=True)
            time.sleep(30)
        """)], stdout=subprocess.PIPE, text=True)
        self.children.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        before = self.live()
        p = self.run_tool("bob", code=1)
        self.assertIn("Codex is writing its login right now", p.stderr)
        self.assertEqual(self.live(), before)

    def test_keyring_store_is_refused_with_the_way_out(self):
        with open(os.path.join(self.codex_home, "config.toml"), "w") as f:
            f.write('cli_auth_credentials_store = "keyring"\n')
        self.set_live(login(**A))
        self.save("bob", login(**B))
        p = self.run_tool("bob", code=1)
        self.assertIn('cli_auth_credentials_store = "file"', p.stderr)

    def test_api_key_login_hops_like_any_other(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        key = {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-abcd1234"}
        self.save("key", key)
        self.run_tool("key")
        self.assertEqual(self.live(), key)
        self.run_tool("ann")
        self.assertEqual(self.live()["auth_mode"], "chatgpt")


class Renew(Base):
    def test_only_due_logins_are_renewed(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        due = login(**B, iat=time.time() - 9.5 * 86400)  # access token runs out in half a day
        self.save("bob", due)
        self.save("cat", login(**C))
        fresh = login(**B)
        self.server.refresh[due["tokens"]["refresh_token"]] = fresh
        p = self.run_tool("renew")
        self.assertIn("bob  access", p.stdout)
        self.assertIn("cat  not due", p.stdout)
        saved = self.read(self.acct("bob"))
        self.assertEqual(saved["tokens"]["refresh_token"], fresh["tokens"]["refresh_token"])
        self.assertEqual(len([c for c in self.server.calls if c[0] == "POST"]), 1)

    def test_a_retired_refresh_token_says_to_sign_in_again(self):
        due = login(**B, iat=time.time() - 9.5 * 86400)
        self.save("bob", due)
        self.server.refresh[due["tokens"]["refresh_token"]] = (
            401, {"error": {"code": "refresh_token_reused", "message": "used"}})
        p = self.run_tool("renew", "bob", code=1)
        self.assertIn("already renewed somewhere else", p.stdout)
        self.assertIn("codexhop add", p.stdout)
        self.assertEqual(self.read(self.acct("bob")), due)

    def test_the_live_login_is_left_to_an_open_codex(self):
        live = login(**A, iat=time.time() - 9.5 * 86400)
        self.set_live(live)
        self.save("ann", live)
        self.fake_running_codex()
        p = self.run_tool("renew")
        self.assertIn("skipped - the live login", p.stdout)
        self.assertEqual([c for c in self.server.calls if c[0] == "POST"], [])

    def test_renewing_the_live_login_writes_both_copies(self):
        live = login(**A, iat=time.time() - 9.5 * 86400)
        self.set_live(live)
        self.save("ann", live)
        fresh = login(**A)
        self.server.refresh[live["tokens"]["refresh_token"]] = fresh
        self.run_tool("renew", "ann")
        self.assertEqual(self.live()["tokens"]["refresh_token"], fresh["tokens"]["refresh_token"])
        saved = self.read(self.acct("ann"))
        self.assertEqual(saved["tokens"]["refresh_token"], fresh["tokens"]["refresh_token"])


class Usage(Base):
    def test_usage_table_and_cache_without_tokens(self):
        a, b = login(**A), login(**B)
        self.set_live(a)
        self.save("ann", a)
        self.save("bob", b)
        self.server.usage[a["tokens"]["access_token"]] = (200, usage_body(12, 30))
        self.server.usage[b["tokens"]["access_token"]] = (200, usage_body(80, 50, plan="pro"))
        p = self.run_tool("usage")
        self.assertRegex(p.stdout, r"ann\s+12%\s+resets")
        self.assertRegex(p.stdout, r"bob\s+80%")
        gets = [c for c in self.server.calls if c[0] == "GET"]
        self.assertEqual({c[2].get("chatgpt-account-id") for c in gets}, {"ws-a", "ws-b"})
        cache = self.text(os.path.join(self.store, "usage.json"))
        for secret in secrets_in(a) + secrets_in(b):
            self.assertNotIn(secret, cache)
        self.server.calls.clear()
        self.run_tool("usage")  # under a minute old: answered from the cache
        self.assertEqual(self.server.calls, [])

    def test_retry_after_is_honoured(self):
        a = login(**A)
        self.save("ann", a)
        self.server.usage[a["tokens"]["access_token"]] = (429, {}, {"Retry-After": "900"})
        p = self.run_tool("usage", "--fresh")
        self.assertIn("throttled", p.stdout)
        self.server.calls.clear()
        p = self.run_tool("usage", "--fresh")
        self.assertIn("throttled", p.stdout)
        self.assertEqual(self.server.calls, [])

    def test_an_idle_expired_login_is_renewed_to_read_it(self):
        stale = login(**B, iat=time.time() - 300 * 3600)
        self.save("bob", stale)
        fresh = login(**B)
        self.server.refresh[stale["tokens"]["refresh_token"]] = fresh
        self.server.usage[fresh["tokens"]["access_token"]] = (200, usage_body(5, 6))
        p = self.run_tool("usage")
        self.assertRegex(p.stdout, r"bob\s+5%")
        saved = self.read(self.acct("bob"))
        self.assertEqual(saved["tokens"]["refresh_token"], fresh["tokens"]["refresh_token"])

    def test_the_live_login_is_not_renewed_under_an_open_codex(self):
        stale = login(**A, iat=time.time() - 300 * 3600)
        self.set_live(stale)
        self.save("ann", stale)
        self.fake_running_codex()
        p = self.run_tool("usage")
        self.assertIn("open codex session renews it", p.stdout)
        self.assertEqual([c for c in self.server.calls if c[0] == "POST"], [])


class Next(Base):
    def test_next_needs_its_list_once_then_picks_the_most_room(self):
        a, b, c = login(**A), login(**B), login(**C)
        self.set_live(a)
        for n, d in (("ann", a), ("bob", b), ("cat", c)):
            self.save(n, d)
        self.server.usage[b["tokens"]["access_token"]] = (200, usage_body(70, 10))
        self.server.usage[c["tokens"]["access_token"]] = (200, usage_body(20, 10))
        p = self.run_tool("next", code=2)
        self.assertIn("which logins it may choose from", p.stderr)
        self.run_tool("next", "ann", "bob", "cat")
        self.assertEqual(self.live(), c)
        self.assertEqual(self.text(os.path.join(self.store, "next")).split(), ["ann", "bob", "cat"])

    def test_next_stops_when_nobody_has_room(self):
        a, b = login(**A), login(**B)
        self.set_live(a)
        self.save("ann", a)
        self.save("bob", b)
        self.server.usage[b["tokens"]["access_token"]] = (200, usage_body(99, 10))
        p = self.run_tool("next", "ann", "bob", code=1)
        self.assertIn("no login in the list has room", p.stderr)
        self.assertEqual(self.live(), a)


class Add(Base):
    def test_add_signs_in_aside_and_leaves_the_live_login(self):
        live = login(**A)
        self.set_live(live)
        self.save("ann", live)
        new = os.path.join(self.tmp, "new.json")
        self.write(new, login(**B))
        p = self.run_tool("add", "bob", "--device-auth", env={"FAKE_CODEX_LOGIN": new})
        self.assertIn("saved bob", p.stdout)
        self.assertEqual(self.live(), live)
        self.assertEqual(self.read(self.acct("bob")), self.read(new))
        call = [c for c in self.codex_calls() if c["argv"][:1] == ["login"]][0]
        self.assertEqual(call["argv"], ["login", "--device-auth"])
        self.assertNotEqual(call["home"], self.codex_home)
        self.assertFalse(os.path.exists(call["home"]))  # the temporary home is gone

    def test_add_with_no_login_makes_it_live(self):
        new = os.path.join(self.tmp, "new.json")
        self.write(new, login(**B))
        p = self.run_tool("add", "bob", env={"FAKE_CODEX_LOGIN": new})
        self.assertIn("now live", p.stdout)
        self.assertEqual(self.live(), self.read(new))

    def test_add_refuses_a_login_already_saved_elsewhere(self):
        self.save("ann", login(**A))
        new = os.path.join(self.tmp, "new.json")
        self.write(new, login(**A))
        p = self.run_tool("add", "work", env={"FAKE_CODEX_LOGIN": new}, code=1)
        self.assertIn("which is saved as 'ann'", p.stderr)
        self.assertFalse(os.path.exists(self.acct("work")))

    def test_add_that_signs_in_to_nothing_saves_nothing(self):
        p = self.run_tool("add", "bob", code=1)
        self.assertIn("Nothing saved", p.stderr)

    def test_a_command_name_is_not_an_account_name(self):
        p = self.run_tool("add", "list", code=1)
        self.assertIn("is a codexhop command", p.stderr)


class Run(Base):
    def setUp(self):
        super().setUp()
        for name, text in (("config.toml", 'model = "x"\n'), ("AGENTS.md", "hello\n"),
                           ("models_cache.json", "{}"), ("history.jsonl", "")):
            with open(os.path.join(self.codex_home, name), "w") as f:
                f.write(text)
        for d in ("sessions", "app-server-daemon", "skills"):
            os.makedirs(os.path.join(self.codex_home, d))
        open(os.path.join(self.codex_home, "auth.lock"), "w").close()
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.bob = login(**B)
        self.save("bob", self.bob)

    def test_run_gives_the_session_a_home_of_its_own(self):
        self.run_tool("run", "bob", "--model", "x")
        call = self.codex_calls()[-1]
        home = os.path.join(self.store, "run", "bob")
        self.assertEqual(call["home"], home)
        self.assertEqual(call["argv"], ["--model", "x"])
        self.assertEqual(self.read(os.path.join(home, "auth.json")), self.bob)
        for linked in ("config.toml", "AGENTS.md", "sessions", "skills", "history.jsonl"):
            self.assertTrue(os.path.islink(os.path.join(home, linked)), linked)
        for kept_out in ("models_cache.json", "app-server-daemon", "codexhop"):
            self.assertFalse(os.path.lexists(os.path.join(home, kept_out)), kept_out)
        self.assertFalse(os.path.islink(os.path.join(home, "auth.lock")))
        self.assertEqual(self.live()["tokens"]["account_id"], "ws-a")  # the live login is untouched

    def test_the_live_account_runs_plain_codex(self):
        self.run_tool("run", "ann")
        self.assertEqual(self.codex_calls()[-1]["home"], self.codex_home)

    def test_a_renewal_inside_the_session_is_saved_back(self):
        renewed = os.path.join(self.tmp, "renewed.json")
        self.write(renewed, login(**B, iat=time.time()))
        self.run_tool("run", "bob", env={"FAKE_CODEX_ROTATE": renewed})
        self.assertEqual(self.read(self.acct("bob")), self.read(renewed))

    def test_signing_in_as_someone_else_inside_is_filed_apart(self):
        other = os.path.join(self.tmp, "other.json")
        self.write(other, login(**C))
        p = self.run_tool("run", "bob", env={"FAKE_CODEX_ROTATE": other})
        self.assertIn("'bob' is unchanged", p.stderr)
        self.assertEqual(self.read(self.acct("bob")), self.bob)
        self.assertEqual(self.read(self.acct("cat")), self.read(other))

    def test_exit_code_passes_through(self):
        self.run_tool("run", "bob", env={"FAKE_CODEX_EXIT": "3"}, code=3)

    def test_codexhop_inside_a_run_session_acts_on_the_real_setup(self):
        self.run_tool("run", "bob")
        inside = os.path.join(self.store, "run", "bob")
        p = self.run_tool("active", env={"CODEX_HOME": inside})
        self.assertEqual(p.stdout.strip(), "ann")


class Doctor(Base):
    def test_clean_setup(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        p = self.run_tool("doctor")
        self.assertIn("no problems found", p.stdout)
        self.assertIn("0.166.0", p.stdout)

    def test_repairs(self):
        mixed = login(**{**A, "field": "ws-b"})
        self.set_live(mixed)
        self.save("ann", login(**A, iat=time.time() - 7200))
        bob = login(**B)
        self.save("bob", bob)
        with open(os.path.join(self.store, "active"), "w") as f:
            f.write("bob\n")
        os.chmod(self.acct("bob"), 0o644)
        os.makedirs(os.path.join(self.store, "run", "gone"))
        p = self.run_tool("doctor", code=1)
        self.assertIn("under the account_id of workspace ws-b", p.stdout)
        self.assertIn("mode 644", p.stdout)
        self.run_tool("doctor", "--fix")
        self.assertEqual(self.live(), bob)
        filed = self.read(self.acct("ann"))
        self.assertEqual(filed["tokens"]["access_token"], mixed["tokens"]["access_token"])
        self.assertEqual(stat.S_IMODE(os.stat(self.acct("bob")).st_mode), 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.store, "run", "gone")))
        self.assertIn("no problems found", self.run_tool("doctor").stdout)

    def test_duplicates_old_codex_and_keyring(self):
        self.save("one", login(**A))
        self.save("two", login(**A))
        with open(os.path.join(self.codex_home, "config.toml"), "w") as f:
            f.write('cli_auth_credentials_store = "auto"\n')
        p = self.run_tool("doctor", code=1, env={"FAKE_CODEX_VERSION": "0.160.1"})
        self.assertIn("are the same login", p.stdout)
        self.assertIn("Codex 0.160.1 does not check", p.stdout)
        self.assertIn("'auto' store", p.stdout)


class Housekeeping(Base):
    def test_rm_and_rename(self):
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        self.run_tool("run", "bob")
        self.run_tool("next", "ann", "bob", code=None)
        self.run_tool("rename", "bob", "robert")
        self.assertTrue(os.path.exists(self.acct("robert")))
        self.assertTrue(os.path.isdir(os.path.join(self.store, "run", "robert")))
        self.assertIn("robert", self.text(os.path.join(self.store, "next")))
        self.run_tool("rm", "robert", "-y")
        self.assertFalse(os.path.exists(self.acct("robert")))
        self.assertFalse(os.path.exists(os.path.join(self.store, "run", "robert")))

    def test_shell_init(self):
        p = self.run_tool("shell-init")
        self.assertIn("alias chop='codexhop'", p.stdout)
        self.assertIn("complete -F _codexhop_complete codexhop chop", p.stdout)
        p = self.run_tool("shell-init", "--alias", "cx")
        self.assertIn("alias cx='codexhop'", p.stdout)
        check = subprocess.run(["bash", "-n"], input=p.stdout, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_unknown_things(self):
        self.assertIn("unknown command", self.run_tool("frobnicate", code=2).stderr)
        self.assertIn("unknown option", self.run_tool("list", "--bogus", code=2).stderr)
        self.assertIn("no saved account called 'nope'", self.run_tool("use", "nope", code=1).stderr)


AZURE_PROFILE = textwrap.dedent('''\
    model_provider = "azure"
    model = "gpt-5.4-mini"

    [model_providers.azure]
    name = "Azure OpenAI"
    base_url = "https://res-a.openai.azure.com/openai/v1"
    env_key = "AZURE_OPENAI_API_KEY"
    wire_api = "responses"
''')


class Providers(Base):
    """Azure, Bedrock and other providers: profile files, a hop that is a pointer, and `codex --profile`."""

    def setUp(self):
        super().setUp()
        self.set_live(login(**A))
        self.save("ann", self.live())
        self.save("bob", login(**B))
        self.ptr = os.path.join(self.store, "provider")

    def profile(self, name: str, text: str = AZURE_PROFILE):
        with open(os.path.join(self.codex_home, f"{name}.config.toml"), "w") as f:
            f.write(text)

    def providers(self) -> dict:
        p = self.run_tool("provider", "list", "--json")
        return {d["name"]: d for d in json.loads(p.stdout)["providers"]}

    def test_a_profile_file_naming_another_provider_is_one(self):
        self.profile("az")
        self.profile("tweak", 'model = "x"\n')  # a profile with no provider is not one
        self.profile("plain", 'model_provider = "openai"\n')  # nor is one that names OpenAI itself
        got = self.providers()
        self.assertEqual(list(got), ["az"])
        self.assertEqual(got["az"]["provider"], "azure")
        self.assertEqual(got["az"]["model"], "gpt-5.4-mini")
        self.assertEqual(got["az"]["baseUrl"], "https://res-a.openai.azure.com/openai/v1")
        self.assertEqual(got["az"]["envKey"], "AZURE_OPENAI_API_KEY")

    def test_providers_sort_by_name(self):
        for n in ("azure", "azure-agents", "azure2"):
            self.profile(n)
        self.assertEqual(list(self.providers()), ["azure", "azure-agents", "azure2"])

    def test_add_writes_a_profile_codex_can_read(self):
        p = self.run_tool("provider", "add", "az", "--azure", "https://res-a.openai.azure.com",
                          "--model", "dep1")
        self.assertIn("AZURE_OPENAI_API_KEY", p.stdout)
        self.run_tool("provider", "add", "bd", "--bedrock", "--region", "us-west-2", "--aws-profile", "work")
        self.run_tool("provider", "add", "local", "--base-url", "http://127.0.0.1:11434/v1",
                      "--env-key", "LOCAL_KEY")
        got = self.providers()
        self.assertEqual(got["az"]["baseUrl"], "https://res-a.openai.azure.com/openai/v1")
        self.assertEqual((got["az"]["model"], got["az"]["envKey"]), ("dep1", "AZURE_OPENAI_API_KEY"))
        self.assertEqual((got["bd"]["provider"], got["bd"]["region"], got["bd"]["awsProfile"]),
                         ("amazon-bedrock", "us-west-2", "work"))
        self.assertIsNone(got["bd"]["model"])
        self.assertEqual((got["local"]["provider"], got["local"]["envKey"]), ("local", "LOCAL_KEY"))
        for n in ("az", "bd", "local"):
            path = os.path.join(self.codex_home, f"{n}.config.toml")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertTrue(self.text(path).startswith("# Written by codexhop"))
            try:
                import tomllib
            except ImportError:
                continue  # Python before 3.11 has no TOML reader to check the file with
            with open(path, "rb") as f:
                self.assertEqual(tomllib.load(f)["model_provider"], got[n]["provider"])

    def test_add_writes_exactly_this(self):
        head = ("# Written by codexhop (codexhop provider add {0}). "
                "`codex --profile {0}` reads it; edit it freely.\n")
        self.run_tool("provider", "add", "az", "--azure", "https://res-a.openai.azure.com", "--model", "dep1")
        self.assertEqual(self.text(os.path.join(self.codex_home, "az.config.toml")), head.format("az") + (
            'model_provider = "azure"\nmodel = "dep1"\n\n[model_providers.azure]\nname = "Azure OpenAI"\n'
            'base_url = "https://res-a.openai.azure.com/openai/v1"\nenv_key = "AZURE_OPENAI_API_KEY"\n'
            'wire_api = "responses"\n'))
        self.run_tool("provider", "add", "bd", "--bedrock", "--region", "us-west-2", "--aws-profile", "work")
        self.assertEqual(self.text(os.path.join(self.codex_home, "bd.config.toml")), head.format("bd") + (
            'model_provider = "amazon-bedrock"\n\n[model_providers.amazon-bedrock.aws]\n'
            'region = "us-west-2"\nprofile = "work"\n'))
        self.run_tool("provider", "add", "local", "--base-url", "http://127.0.0.1:11434/v1",
                      "--env-key", "LOCAL_KEY")
        local = os.path.join(self.codex_home, "local.config.toml")
        self.assertEqual(self.text(local), head.format("local") + (
            'model_provider = "local"\n\n[model_providers.local]\nname = "local"\n'
            'base_url = "http://127.0.0.1:11434/v1"\nenv_key = "LOCAL_KEY"\n'
            'wire_api = "responses"\n'))
        self.run_tool("provider", "add", "local2", "--base-url", "http://127.0.0.1:1/v1")
        self.assertNotIn("env_key", self.text(os.path.join(self.codex_home, "local2.config.toml")))

    def test_add_refuses_a_query_and_a_bad_launcher_and_writes_nothing(self):
        p = self.run_tool("provider", "add", "q", "--azure", "https://r.openai.azure.com/openai/v1?api-version=1",
                          "--model", "d", code=2)
        self.assertIn("no ?query", p.stderr)
        p = self.run_tool("provider", "add", "l", "--bedrock", "--launcher", "'unterminated", code=2)
        self.assertIn("not a command", p.stderr)
        for n in ("q", "l"):
            self.assertFalse(os.path.exists(os.path.join(self.codex_home, f"{n}.config.toml")))

    def test_a_key_is_never_taken_or_printed(self):
        looks_like_a_name = "Zq" + "x9" * 14  # 30 letters and digits, no underscore
        has_dashes = "sk-test-" + "a1B2c3D4e5F6g7H8i9J0"
        for value in (looks_like_a_name, has_dashes):
            p = self.run_tool("provider", "add", "x", "--bedrock", "--env-key", value, code=2)
            p2 = self.run_tool("provider", "add", "x", "--base-url", "http://h/v1", "--env-key", value,
                               code=2)
            for out in (p, p2):
                self.assertNotIn(value, out.stdout + out.stderr)
                self.assertIn("--env-key", out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))
        secret = "hunter2" + "Q" * 20
        p = self.run_tool("provider", "add", "x", "--base-url", f"https://user:{secret}@h.example/v1", code=2)
        self.assertNotIn(secret, p.stdout + p.stderr)
        self.assertIn("user name or password", p.stderr)

    def test_a_key_in_a_hand_written_profile_is_not_shown(self):
        key, pw = "Zq" + "x9" * 14, "pw" + "R" * 16
        self.profile("hand", AZURE_PROFILE.replace('"AZURE_OPENAI_API_KEY"', f'"{key}"').replace(
            "https://res-a.openai.azure.com", f"https://u:{pw}@res-a.openai.azure.com") + "")
        shown = "".join(self.run_tool(*a, env={"AZURE_OPENAI_API_KEY": "x"}).stdout for a in (
            ("provider", "list"), ("provider", "list", "--json"), ("list",), ("list", "--json"), ("doctor",)))
        self.assertNotIn(key, shown)
        self.assertNotIn(pw, shown)
        self.assertIn("res-a.openai.azure.com", shown)
        self.assertRegex(self.run_tool("doctor").stdout,
                         r"provider 'hand' has an env_key that looks like a key")

    def all_output(self, name: str) -> str:
        """Everything the tool prints about a provider, through each command that lists or starts one."""
        out = []
        for args in (("provider", "list"), ("provider", "list", "--json"), ("list",), ("list", "--json"),
                     ("list", "--long"), ("whoami",), ("whoami", "--json"), ("doctor",), ("doctor", "--json"),
                     ("active", "--json"), (name,), ("launch", "--", "exec"), ("run", name)):
            p = self.run_tool(*args, code=None, env={"AZURE_OPENAI_API_KEY": "x"})
            out.append(p.stdout + p.stderr)
        return "".join(out)

    def test_a_key_with_underscores_is_neither_taken_nor_shown(self):
        key = "gsk_" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6nP9r"  # made up, 44 characters
        p = self.run_tool("provider", "add", "x", "--base-url", "http://h/v1", "--env-key", key, code=2)
        self.assertNotIn(key, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))
        self.profile("hand", AZURE_PROFILE.replace('"AZURE_OPENAI_API_KEY"', f'"{key}"'))
        self.assertNotIn(key, self.all_output("hand"))
        self.assertIsNone(self.providers()["hand"]["envKey"])

    def test_only_a_variable_name_is_taken(self):
        for i, var in enumerate(("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_KEY_2024", "my_vault_key",
                                 "AWS_BEARER_TOKEN_BEDROCK")):
            self.run_tool("provider", "add", f"p{i}", "--base-url", "http://h/v1", "--env-key", var)
            self.assertEqual(self.providers()[f"p{i}"]["envKey"], var)
        for var in ("my-key", "my key", "1KEY", "MY_KEY=1"):  # short, so only the name rule can refuse them
            p = self.run_tool("provider", "add", "x", "--base-url", "http://h/v1", "--env-key", var, code=2)
            self.assertIn("is not a variable name", p.stderr)
        # each of these is refused by one rule alone: mixed case, digits, length, no underscore in a long one
        for var in ("AzureKey", "ABCD12345", "AAAAA_" * 7, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            p = self.run_tool("provider", "add", "x", "--base-url", "http://h/v1", "--env-key", var,
                              code=2)
            self.assertIn("looks like a key", p.stderr)
            self.assertNotIn(var, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))

    def test_the_query_and_fragment_of_an_address_are_not_listed(self):
        self.profile("hand", AZURE_PROFILE.replace(
            "/openai/v1", "/openai/v1?api-key=QUERYSECRET#FRAGSECRET"))
        shown = self.all_output("hand")
        for secret in ("QUERYSECRET", "FRAGSECRET", "api-key"):
            self.assertNotIn(secret, shown)
        self.assertEqual(self.providers()["hand"]["baseUrl"], "https://res-a.openai.azure.com/openai/v1")

    def test_a_password_in_the_path_of_an_address_with_no_host_is_not_shown(self):
        # urlsplit reads "https:////user:pw@host/v1" as no host and a path, and putting it back
        # together gives the password back unless the result is read again and checked
        for bad in ("https:////user:PWSECRET@res-a.openai.azure.com/openai/v1", "http://u:PWSECRET@"):
            self.profile("hand", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", bad))
            self.assertNotIn("PWSECRET", self.all_output("hand"), bad)
            self.assertIsNone(self.providers()["hand"]["baseUrl"])

    def test_an_address_with_an_odd_port_or_look_alike_is_not_shown(self):
        # a "port" that is not a number may be a password; urlsplit rejects a full-width @ in a host
        for bad in ("https://res-a.openai.azure.com:PWSECRET/openai/v1",
                    "https://user:PWSECRET\uff20res-a.openai.azure.com/openai/v1"):
            self.profile("hand", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", bad))
            self.assertNotIn("PWSECRET", self.all_output("hand"), bad)
            self.assertIsNone(self.providers()["hand"]["baseUrl"], bad)

    def test_a_host_with_non_ascii_letters_is_shown(self):
        # letters, and letters with combining marks, are what an internationalised name is made of
        for host in ("b\u00fccher.example", "\u4f8b\u3048.jp", "cafe\u0301.example"):
            url = f"https://{host}/v1/caf\u00e9"
            self.profile("hand", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", url))
            self.assertEqual(self.providers()["hand"]["baseUrl"], url, host)
            p = self.run_tool("provider", "add", "idn", "--base-url", f"https://{host}/v1")
            self.assertIn(host, p.stdout)
            self.run_tool("provider", "rm", "idn", "-y")

    def test_a_colon_look_alike_in_a_host_is_not_shown_or_written(self):
        # U+2236 and U+A789 are not folded to ":" by urlsplit, so a password could hide after them
        for mark in ("\u2236", "\ua789", "\uff1a", "\u2215"):
            bad = f"https://res-a.openai.azure.com{mark}PWSECRET/openai/v1"
            self.profile("hand", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", bad))
            self.assertNotIn("PWSECRET", self.all_output("hand"), mark)
            self.assertIsNone(self.providers()["hand"]["baseUrl"], mark)
            p = self.run_tool("provider", "add", "x", "--base-url", bad, code=2)
            self.assertNotIn("PWSECRET", p.stdout + p.stderr)
            self.assertIn("host and the port", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))

    def test_an_address_with_a_port_but_no_host_is_refused_and_not_shown(self):
        for bad in ("https://:80/v1", "https://:/v1"):
            self.profile("hand", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", bad))
            self.assertIsNone(self.providers()["hand"]["baseUrl"], bad)
            p = self.run_tool("provider", "add", "x", "--base-url", bad, code=2)
            self.assertIn("host and the port", p.stderr, bad)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))

    def test_only_a_plain_port_in_range_is_shown_or_written(self):
        # Python 3.9 reads "+80" and "1_0" as ports; a port is digits, 0 to 65535, and at most one
        for port in ("+80", "1_0", "80:90", "65536", "-1", "0x50", "PWSECRET", "\u0668\u0660"):
            bad = f"https://res-a.openai.azure.com:{port}/openai/v1"
            self.profile("hand", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", bad))
            self.assertIsNone(self.providers()["hand"]["baseUrl"], port)
            p = self.run_tool("provider", "add", "x", "--base-url", bad, code=2)
            self.assertIn("host and the port", p.stderr, port)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))
        for good in ("https://h:65535/v1", "https://h:8080/v1", "http://[::1]:11434/v1", "https://h:/v1"):
            self.profile("hand", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", good))
            self.assertEqual(self.providers()["hand"]["baseUrl"], good)

    def test_add_refuses_a_port_that_is_not_a_number_and_writes_nothing(self):
        p = self.run_tool("provider", "add", "x", "--base-url", "https://h:PWSECRET/v1", code=2)
        self.assertNotIn("PWSECRET", p.stdout + p.stderr)
        self.assertIn("host and the port", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))

    def test_a_providers_file_of_the_wrong_shape_does_not_crash_anything(self):
        self.profile("az")
        for entry in ({"launcher": 5}, {"launcher": ["codex"]}, {"launcher": True}, {"launcher": "   "},
                      {"launcher": "\n"}, 5, ["x"], "codex"):
            with open(os.path.join(self.store, "providers.json"), "w") as f:
                json.dump({"az": entry}, f)
            for args in (("provider", "list"), ("provider", "list", "--json"), ("list", "--json"),
                         ("doctor",)):
                p = self.run_tool(*args, code=None)
                self.assertNotIn("Traceback", p.stderr, (entry, args))
            self.assertIsNone(self.providers()["az"]["launcher"], entry)
            self.run_tool("run", "az")  # a blank launcher is no launcher: codex runs on the profile
            self.assertEqual(self.codex_calls()[-1]["argv"], ["--profile", "az"], entry)
            self.run_tool("provider", "set", "az", "--launcher", "codex --via-launcher")  # replaces it
            self.assertEqual(self.providers()["az"]["launcher"], "codex --via-launcher", entry)

    def test_a_providers_file_that_will_not_parse_is_never_overwritten(self):
        path = os.path.join(self.store, "providers.json")
        self.run_tool("provider", "add", "mine", "--bedrock", "--launcher", "codex --one")
        self.run_tool("provider", "add", "other", "--bedrock", "--launcher", "codex --two")
        broken = self.text(path).rstrip().rstrip("}") + ",}"  # a trailing comma
        for text in (broken, "[" * 300000):  # and one nested too deeply to read
            with open(path, "w") as f:
                f.write(text)
            self.assertEqual(self.run_tool("provider", "list", code=0).returncode, 0)
            for args in (("provider", "set", "mine", "--launcher", "codex --three"),
                         ("provider", "rm", "mine", "-y"),
                         ("provider", "add", "new", "--bedrock", "--launcher", "codex --four")):
                p = self.run_tool(*args, code=1)
                self.assertIn("Refusing to touch it", p.stderr, args)
                self.assertNotIn("Traceback", p.stderr, args)
            self.assertEqual(self.text(path), text)  # not rewritten with the new entry alone
            self.assertTrue(os.path.exists(os.path.join(self.codex_home, "mine.config.toml")))  # not deleted
            self.assertFalse(os.path.exists(os.path.join(self.codex_home, "new.config.toml")))  # not written

    def test_a_providers_file_that_cannot_be_used_stops_every_change(self):
        # not UTF-8, a JSON list, and a directory where the file should be
        path = os.path.join(self.store, "providers.json")
        self.run_tool("provider", "add", "mine", "--bedrock", "--launcher", "codex --one")

        def raw() -> bytes | None:
            if os.path.isdir(path):
                return None
            with open(path, "rb") as f:
                return f.read()

        for kind in ("bytes", "list", "dir"):
            if os.path.isdir(path):
                os.rmdir(path)
            elif os.path.exists(path):
                os.remove(path)
            if kind == "dir":
                os.mkdir(path)
            else:
                with open(path, "wb") as f:
                    f.write(b'{"mine": {"launcher": "\xff\xfe"}}' if kind == "bytes"
                            else b'[{"launcher": "codex --one"}]')
            before = raw()
            for args in (("provider", "set", "mine", "--launcher", "codex --three"),
                         ("provider", "rm", "mine", "-y"),
                         ("provider", "add", "new", "--bedrock", "--launcher", "codex --four")):
                p = self.run_tool(*args, code=1)
                self.assertIn("Refusing to touch it", p.stderr, (kind, args))
                self.assertNotIn("Traceback", p.stderr, (kind, args))
            self.assertEqual(raw(), before, kind)  # not rewritten
            self.assertEqual(os.path.isdir(path), kind == "dir", kind)
            self.assertTrue(os.path.exists(os.path.join(self.codex_home, "mine.config.toml")), kind)
            self.assertFalse(os.path.exists(os.path.join(self.codex_home, "new.config.toml")), kind)
            for args in (("provider", "list"), ("provider", "list", "--json"), ("list", "--json")):
                p = self.run_tool(*args, code=None)
                self.assertNotIn("Traceback", p.stderr, (kind, args))
                self.assertEqual(p.returncode, 0, (kind, args))

    def test_a_run_and_doctor_say_when_the_providers_file_cannot_be_used(self):
        self.profile("az")
        for args in (("run", "az"), ("launch", "--")):
            self.run_tool("az")
            self.assertNotIn("cannot be read", self.run_tool(*args).stderr)  # no file: nothing to say
        self.assertNotIn("providers.json", self.run_tool("doctor", code=None).stdout)
        path = os.path.join(self.store, "providers.json")
        trailing_comma = '{"az": {"launcher": "codex --via-launcher",}}'
        for text, why in ((trailing_comma, "cannot be read (JSONDecodeError)"),
                          ('["x"]', "is not a JSON object")):
            with open(path, "w") as f:
                f.write(text)
            for args in (("run", "az"), ("launch", "--")):
                self.run_tool("az")
                p = self.run_tool(*args)
                self.assertIn(f"providers.json {why}, so a launcher set for 'az' is not used", p.stderr, args)
                self.assertEqual(self.codex_calls()[-1]["argv"][:2], ["--profile", "az"])  # no launcher ran
            out = self.run_tool("doctor", code=None).stdout
            self.assertIn(f"providers.json {why}, so launchers set with `codexhop provider set` are not "
                          f"used", out)
            self.assertNotIn("codex --via-launcher", out)
        with open(path, "w") as f:
            f.write('{"az": {"launcher": "codex --via-launcher"}}')
        self.assertNotIn("providers.json", self.run_tool("doctor", code=None).stdout)
        self.run_tool("az")
        self.assertNotIn("not used", self.run_tool("launch", "--").stderr)

    def test_an_unknown_option_is_named_without_its_value(self):
        value = "sk-test-" + "zZ9" * 10
        for opt in (f"--api-key={value}", f"-k={value}"):
            p = self.run_tool("list", opt, code=2)
            self.assertNotIn(value, p.stdout + p.stderr)
            self.assertIn(f"unknown option '{opt.split('=')[0]}'", p.stderr)

    def test_doctor_does_not_say_codex_finds_no_key_for_a_name_it_only_hides(self):
        self.profile("hand", AZURE_PROFILE.replace('"AZURE_OPENAI_API_KEY"', '"AzureKey"'))
        out = self.run_tool("doctor").stdout
        self.assertIn("provider 'hand' has an env_key that looks like a key", out)
        self.assertIn("does not list it or check that it is set. If that value is a key, take it out of", out)
        self.assertNotIn("find no key", out)

    def test_a_malformed_address_does_not_crash_anything(self):
        # the second address splits fine until its user name is cut off, which leaves "http://[aa::1"
        for bad in ("http://[bad", "http://]a::1x@[aa::1"):
            self.profile("bad", AZURE_PROFILE.replace("https://res-a.openai.azure.com/openai/v1", bad))
            for args in (("list",), ("provider", "list"), ("provider", "list", "--json"), ("list", "--json"),
                         ("whoami",), ("doctor",), ()):
                p = self.run_tool(*args, code=None)
                self.assertNotIn("Traceback", p.stderr, (bad, args))
            self.assertEqual(self.run_tool("provider", "list").returncode, 0)
            self.assertIsNone(self.providers()["bad"]["baseUrl"], bad)
        p = self.run_tool("provider", "add", "y", "--azure", "http://[bad", "--model", "d", code=2)
        self.assertNotIn("Traceback", p.stderr)

    def test_an_unknown_option_never_repeats_its_value(self):
        value = "sk-test-" + "zZ9" * 10
        for extra in (["--api-key", value], [f"--api-key={value}"], ["--a=" + value], ["--env-keys", value]):
            p = self.run_tool("provider", "add", "x", "--base-url", "http://h/v1", *extra, code=2)
            self.assertNotIn(value, p.stdout + p.stderr, extra)
            self.assertIn("unrecognized arguments", p.stderr)
        p = self.run_tool("provider", "add", "x", "--base-url", "http://h/v1", "--api-key", value, code=2)
        self.assertIn("--api-key", p.stderr)  # the flag is named, so a typo can be found
        self.profile("az")
        p = self.run_tool("provider", "set", "az", "--launcher", "codex", value, code=2)
        self.assertNotIn(value, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))

    def test_a_launcher_cannot_carry_a_key(self):
        value = "sk-test-" + "zZ9" * 10
        self.profile("az")
        for launcher in (f"env AZURE_OPENAI_API_KEY={value} codex", f"env my_token='{value}' codex",
                         f"env X=1 AWS_BEARER_TOKEN_BEDROCK={value} codex",
                         # each of these names matches one word of the rule and no other
                         f"env MY_AUTH={value} codex", f"env APP_SECRET={value} codex",
                         f"env DB_PASSWD={value} codex", f"env CLOUD_CREDENTIALS={value} codex",
                         # a path excuses a name with no KEY, TOKEN or SECRET in it, and nothing else
                         f"env AWS_SECRET_ACCESS_KEY=/{value} codex", f"env X_TOKEN=./{value} codex",
                         f"env APP_AUTH=~{value} codex", f"env APP_AUTH=.{value} codex",
                         # a value can run over several lines
                         "env " + shlex.quote('GOOGLE_CREDENTIALS={"k":\n"' + value + '"}') + " codex"):
            p = self.run_tool("provider", "set", "az", "--launcher", launcher, code=2)
            self.assertNotIn(value, p.stdout + p.stderr)
            self.assertRegex(p.stderr, r"sets \w+ to a value")
            p = self.run_tool("provider", "add", "x", "--bedrock", "--launcher", launcher, code=2)
            self.assertNotIn(value, p.stdout + p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "x.config.toml")))
        self.assertFalse(os.path.exists(os.path.join(self.store, "providers.json")))

    def test_a_launcher_that_is_not_a_command_is_refused_and_never_crashes_a_run(self):
        self.profile("az")
        for launcher in ("''", "  ", '""'):  # a NUL cannot be an argument; the hand-edited case is below
            p = self.run_tool("provider", "set", "az", "--launcher", launcher, code=2)
            self.assertIn("not a command", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.store, "providers.json")))
        for launcher in ("''", "\u0000codex"):  # written by hand
            with open(os.path.join(self.store, "providers.json"), "w") as f:
                json.dump({"az": {"launcher": launcher}}, f)
            for args in (("run", "az"), ("launch", "--")):
                self.run_tool("az")
                p = self.run_tool(*args, code=1)
                self.assertNotIn("Traceback", p.stderr, (launcher, args))
                self.assertIn("is not a command", p.stderr, (launcher, args))

    def test_escapes_in_a_hand_written_profile_are_read(self):
        # Python 3.9 and 3.10 have no TOML reader, so this checks their line reader as well as tomllib
        text = AZURE_PROFILE.replace('model = "gpt-5.4-mini"', 'model = "m\\u00f6del-\\"x\\""')
        self.profile("hand", text.replace("https://res-a.openai.azure.com/openai/v1",
                                          "https://b\\u00fccher.example/v1"))
        got = self.providers()["hand"]
        self.assertEqual(got["model"], 'm\u00f6del-"x"')
        self.assertEqual(got["baseUrl"], "https://b\u00fccher.example/v1")
        self.profile("lit", AZURE_PROFILE.replace('model = "gpt-5.4-mini"', "model = 'a\\u00f6b'"))
        self.assertEqual(self.providers()["lit"]["model"], "a\\u00f6b")  # a literal string has no escapes

    def test_a_launcher_may_set_other_variables(self):
        self.profile("az")
        for launcher in ("env CODEX_AZURE_PROFILE=azure-agents codex", "env AZURE_KEY_FILE=k.txt codex",
                         "env GOOGLE_APPLICATION_CREDENTIALS=/home/me/key.json codex",
                         "env SSH_AUTH_SOCK=./sock codex", "env PASSWORD_STORE_DIR=~/pw codex",
                         "env AZURE_OPENAI_API_KEY= codex"):  # nothing after the = to hide
            self.run_tool("provider", "set", "az", "--launcher", launcher)
            self.assertEqual(self.providers()["az"]["launcher"], launcher)
        self.run_tool("az")
        self.run_tool("launch", "--", "exec")
        self.assertEqual(self.codex_calls()[-1]["argv"], ["exec"])  # `env ... codex` ran the fake codex

    def test_a_key_in_a_hand_edited_launcher_is_masked_but_still_runs(self):
        value = "sk-test-" + "zZ9" * 10
        proof = os.path.join(self.codex_home, "proof.txt")
        self.profile("az")
        script = f'printf %s "$AZURE_OPENAI_API_KEY" > {proof}'
        launcher = f"env AZURE_OPENAI_API_KEY={value} sh -c {shlex.quote(script)} sh"
        with open(os.path.join(self.store, "providers.json"), "w") as f:
            json.dump({"az": {"launcher": launcher}}, f)
        shown = self.all_output("az")
        self.assertNotIn(value, shown)
        listed = self.providers()["az"]["launcher"]
        self.assertIn("AZURE_OPENAI_API_KEY=***", listed)
        self.assertNotIn(value, listed)
        self.assertIn("launcher: env AZURE_OPENAI_API_KEY=***", self.run_tool("provider", "list").stdout)
        self.assertEqual(self.text(proof), value)  # masked for listing only; the launcher ran as written
        with open(os.path.join(self.store, "providers.json"), "w") as f:
            json.dump({"az": {"launcher": f"env AZURE_OPENAI_API_KEY={value} 'unterminated"}}, f)
        self.assertNotIn(value, self.all_output("az"))
        self.assertIsNone(self.providers()["az"]["launcher"])
        self.assertIn("(unreadable)", self.run_tool("provider", "list").stdout)

    def test_add_refuses_what_would_not_work(self):
        self.run_tool("provider", "add", "az", "--azure", "https://res-a.openai.azure.com", "--model", "d")
        self.assertIn("already a provider", self.run_tool(
            "provider", "add", "az", "--azure", "https://r.openai.azure.com", "--model", "d", code=1).stderr)
        self.profile("tweak", 'model = "x"\n')  # a profile file that is not a provider is still in the way
        self.assertIn("already exists", self.run_tool(
            "provider", "add", "tweak", "--bedrock", code=1).stderr)
        self.assertIn("saved account", self.run_tool(
            "provider", "add", "ann", "--bedrock", code=1).stderr)
        self.assertIn("needs --model", self.run_tool(
            "provider", "add", "x", "--azure", "https://r.openai.azure.com", code=2).stderr)
        self.assertIn("belong to --bedrock", self.run_tool(
            "provider", "add", "x", "--base-url", "http://h/v1", "--region", "r", code=2).stderr)
        self.assertIn("does not apply", self.run_tool(
            "provider", "add", "x", "--bedrock", "--env-key", "K", code=2).stderr)
        self.assertIn("built into Codex", self.run_tool(
            "provider", "add", "ollama", "--base-url", "http://h/v1", code=2).stderr)
        self.assertIn("could never hop", self.run_tool(
            "provider", "add", "list", "--bedrock", code=1).stderr)
        self.run_tool("provider", "add", "x", "--azure", "not a url", "--model", "d", code=2)

    def test_an_account_cannot_take_a_providers_name(self):
        self.profile("az")
        p = self.run_tool("rename", "bob", "az", code=1)
        self.assertIn("already a provider", p.stderr)

    def test_hopping_to_a_provider_is_a_pointer_and_leaves_the_login_alone(self):
        self.profile("az")
        before = self.text(os.path.join(self.codex_home, "auth.json"))
        p = self.run_tool("az")
        self.assertIn("switched to az", p.stdout)
        self.assertEqual(self.text(self.ptr).strip(), "az")
        self.assertEqual(self.text(os.path.join(self.codex_home, "auth.json")), before)
        self.assertEqual(self.run_tool("active").stdout.strip(), "az")
        self.assertEqual(json.loads(self.run_tool("active", "--json").stdout)["active"], "ann")
        self.assertIn("already on az", self.run_tool("az").stdout)

    def test_hopping_back_to_an_account_drops_the_pointer(self):
        self.profile("az")
        self.run_tool("az")
        p = self.run_tool("ann")
        self.assertIn("left provider az", p.stdout)
        self.assertFalse(os.path.exists(self.ptr))
        self.run_tool("az")
        p = self.run_tool("bob")
        self.assertIn("left provider az", p.stdout)
        self.assertFalse(os.path.exists(self.ptr))
        self.assertEqual(self.live()["tokens"]["account_id"], "ws-b")
        self.assertEqual(self.run_tool("active").stdout.strip(), "bob")

    def test_the_listing_stars_what_new_sessions_use(self):
        self.profile("az")
        starred = lambda out: [ln for ln in out.splitlines() if ln.startswith("*")]  # noqa: E731
        out = self.run_tool("list").stdout
        self.assertIn("EMAIL / PROVIDER", out)
        self.assertIn("azure, res-a.openai.azure.com, gpt-5.4-mini", out)
        self.assertTrue(starred(out)[0].split()[1] == "ann")
        self.run_tool("az")
        out = self.run_tool("list").stdout
        self.assertEqual([ln.split()[1] for ln in starred(out)], ["az"])
        who = self.run_tool("whoami").stdout
        self.assertIn("hopped to provider az", who)
        self.assertEqual(json.loads(self.run_tool("whoami", "--json").stdout)["provider"], "az")

    def test_whoami_with_a_provider_and_no_chatgpt_login(self):
        self.profile("az")
        os.remove(os.path.join(self.codex_home, "auth.json"))
        self.run_tool("whoami", code=1)
        self.run_tool("az")
        self.assertIn("provider az", self.run_tool("whoami").stdout)

    def test_launch_follows_the_pointer(self):
        self.profile("az")
        self.run_tool("launch", "--", "exec", "hi")
        self.assertEqual(self.codex_calls()[-1]["argv"], ["exec", "hi"])  # nothing hopped to: plain codex
        self.run_tool("az")
        self.run_tool("launch", "--", "exec", "hi")
        self.assertEqual(self.codex_calls()[-1]["argv"], ["--profile", "az", "exec", "hi"])
        self.run_tool("launch", "--", "fix the bug")
        self.assertEqual(self.codex_calls()[-1]["argv"], ["--profile", "az", "fix the bug"])
        self.assertEqual(self.codex_calls()[-1]["home"], self.codex_home)

    def test_launch_leaves_commands_that_take_no_profile_alone(self):
        self.profile("az")
        self.run_tool("az")
        for args in (["logout"], ["update"], ["doctor"], ["--profile", "other"], ["-p", "other", "exec"]):
            self.run_tool("launch", "--", *args)
            self.assertEqual(self.codex_calls()[-1]["argv"], args)

    def test_launch_gives_debug_prompt_input_a_profile_and_help_and_version_none(self):
        self.profile("az")
        self.run_tool("az")
        self.run_tool("launch", "--", "debug", "prompt-input", "hi")
        self.assertEqual(self.codex_calls()[-1]["argv"], ["--profile", "az", "debug", "prompt-input", "hi"])
        for args in (["debug", "models"], ["--version"], ["-V"], ["exec", "--help"], ["-h"]):
            p = self.run_tool("launch", "--", *args)
            self.assertEqual(self.codex_calls()[-1]["argv"], args)
            self.assertNotIn("is not set", p.stderr)  # no key warning for a command that needs no key

    def test_launch_passes_the_exit_code_through(self):
        self.profile("az")
        self.run_tool("az")
        self.run_tool("launch", "--", "exec", env={"FAKE_CODEX_EXIT": "3"}, code=3)

    def test_a_launcher_starts_codex_instead(self):
        self.profile("az")
        self.run_tool("provider", "set", "az", "--launcher", "codex --via-launcher")
        self.assertEqual(self.providers()["az"]["launcher"], "codex --via-launcher")
        self.run_tool("az")
        self.run_tool("launch", "--", "exec")
        self.assertEqual(self.codex_calls()[-1]["argv"], ["--via-launcher", "exec"])
        self.run_tool("run", "az", "review")
        self.assertEqual(self.codex_calls()[-1]["argv"], ["--via-launcher", "review"])
        self.run_tool("provider", "set", "az", "--launcher", "")
        self.assertIsNone(self.providers()["az"]["launcher"])

    def test_run_uses_the_provider_in_this_terminal_only(self):
        self.profile("az")
        self.run_tool("run", "az", "exec", "hi")
        call = self.codex_calls()[-1]
        self.assertEqual(call["argv"], ["--profile", "az", "exec", "hi"])
        self.assertEqual(call["home"], self.codex_home)  # no home of its own: there is no login to keep apart
        self.assertFalse(os.path.exists(self.ptr))
        self.assertFalse(os.path.exists(os.path.join(self.store, "run", "az")))

    def test_run_refuses_what_a_profile_cannot_do(self):
        self.profile("az")
        self.assertIn("does not use a profile", self.run_tool("run", "az", "login", code=2).stderr)
        p = self.run_tool("run", "az", "--profile", "x", code=2)
        self.assertIn("already picks the profile", p.stderr)

    def test_a_key_that_is_not_set_is_said_so(self):
        self.profile("az")
        p = self.run_tool("run", "az")
        self.assertIn("$AZURE_OPENAI_API_KEY is not set", p.stderr)
        key = "zK9" + "x" * 30
        p = self.run_tool("run", "az", env={"AZURE_OPENAI_API_KEY": key})
        self.assertNotIn("is not set", p.stderr)
        self.assertNotIn(key, p.stdout + p.stderr)  # the value is never printed
        p = self.run_tool("provider", "list")
        listed = self.run_tool("provider", "list", env={"AZURE_OPENAI_API_KEY": key})
        self.assertIn("$AZURE_OPENAI_API_KEY set", listed.stdout)
        self.assertNotIn(key, listed.stdout + listed.stderr)
        self.assertIn("not set", p.stdout)

    def test_rm_deletes_only_what_codexhop_wrote(self):
        self.profile("mine")
        p = self.run_tool("provider", "rm", "mine", "-y", code=1)
        self.assertIn("was not written by codexhop", p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.codex_home, "mine.config.toml")))
        self.run_tool("provider", "add", "bd", "--bedrock", "--region", "us-west-2")
        self.run_tool("provider", "set", "bd", "--launcher", "codex --via-launcher")
        self.assertIn("bd", self.read(os.path.join(self.store, "providers.json")))
        self.run_tool("bd")
        self.run_tool("rm", "bd", "-y")
        self.assertFalse(os.path.exists(os.path.join(self.codex_home, "bd.config.toml")))
        self.assertFalse(os.path.exists(self.ptr))
        left = self.read(os.path.join(self.store, "providers.json"))
        self.assertNotIn("bd", left)  # no launcher left behind

    def test_doctor_reports_a_stale_pointer_and_a_name_clash(self):
        self.profile("az")
        self.run_tool("az")
        os.remove(os.path.join(self.codex_home, "az.config.toml"))
        self.assertIn("whose profile file", self.run_tool("doctor").stdout)
        self.assertEqual(self.run_tool("active").stdout.strip(), "ann")  # a pointer to nothing is ignored
        self.run_tool("doctor", "--fix")
        self.assertFalse(os.path.exists(self.ptr))
        self.profile("ann")  # a profile with an account's name
        self.assertIn("both a saved account and a provider", self.run_tool("doctor").stdout)
        p = self.run_tool("ann")  # the account wins: it is the live login, and no pointer is set
        self.assertIn("already on ann", p.stdout)
        self.assertFalse(os.path.exists(self.ptr))
        p = self.run_tool("bob")
        self.assertIn("switched to bob", p.stdout)
        self.assertEqual(self.live()["tokens"]["account_id"], "ws-b")
        self.assertFalse(os.path.exists(self.ptr))

    def test_doctor_names_the_key_variable_that_is_not_set(self):
        self.profile("az")
        self.run_tool("az")
        glue = {"CODEXHOP_WRAP": "1"}
        out = self.run_tool("doctor", env=glue).stdout
        self.assertIn("provider 'az' reads its key from $AZURE_OPENAI_API_KEY, which is not set", out)
        out = self.run_tool("doctor", env={**glue, "AZURE_OPENAI_API_KEY": "zK9" + "x" * 30}).stdout
        self.assertNotIn("reads its key from", out)
        self.assertNotIn("zK9", out)

    def test_doctor_says_when_the_shell_glue_is_missing(self):
        self.profile("az")
        self.run_tool("az")
        out = self.run_tool("doctor", env={"AZURE_OPENAI_API_KEY": "k"}).stdout
        self.assertIn("shell-init --wrap-codex", out)
        out = self.run_tool("doctor", env={"AZURE_OPENAI_API_KEY": "k", "CODEXHOP_WRAP": "1"}).stdout
        self.assertNotIn("shell glue", out)

    def test_hop_says_when_plain_codex_will_not_follow(self):
        self.profile("az")
        p = self.run_tool("az")
        self.assertIn("--wrap-codex", p.stderr)
        self.run_tool("ann")
        p = self.run_tool("az", env={"CODEXHOP_WRAP": "1"})
        self.assertNotIn("--wrap-codex", p.stderr)
        self.assertIn("new `codex` sessions start on this provider", p.stdout)

    def test_the_codex_function_follows_the_hop(self):
        """The real glue in a real bash: `codex` runs on the provider after a hop, plain after a hop back."""
        self.profile("az")
        shim = os.path.join(self.bin, "codexhop")
        with open(shim, "w") as f:
            f.write(f'#!/bin/sh\nexec "{PYTHON}" "{TOOL}" "$@"\n')
        os.chmod(shim, 0o755)
        script = 'eval "$(codexhop shell-init --wrap-codex)"; codex exec one; codexhop az >/dev/null 2>&1; ' \
                 'codex exec two; codex logout; codexhop ann >/dev/null 2>&1; codex exec three'
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([c["argv"] for c in self.codex_calls() if c["argv"][:1] != ["--version"]], [
            ["exec", "one"], ["--profile", "az", "exec", "two"], ["logout"], ["exec", "three"]])

    def test_the_codex_function_keeps_arguments_whole(self):
        self.profile("az")
        shim = os.path.join(self.bin, "codexhop")
        with open(shim, "w") as f:
            f.write(f'#!/bin/sh\nexec "{PYTHON}" "{TOOL}" "$@"\n')
        os.chmod(shim, 0o755)
        script = ('eval "$(codexhop shell-init --wrap-codex)"; codexhop az >/dev/null 2>&1; '
                  "codex exec 'two words' \"it's\" '$HOME' '*' '' -x -- --")
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.codex_calls()[-1]["argv"], [
            "--profile", "az", "exec", "two words", "it's", "$HOME", "*", "", "-x", "--", "--"])

    def test_shell_init_wrap_codex_is_opt_in(self):
        self.assertNotIn("codex()", self.run_tool("shell-init").stdout)
        out = self.run_tool("shell-init", "--wrap-codex").stdout
        self.assertIn("codex() {", out)
        self.assertIn("CODEXHOP_WRAP=1", out)
        check = subprocess.run(["bash", "-n"], input=out, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_the_menu_offers_providers_and_hops_to_the_one_you_pick(self):
        import pty
        import select

        self.profile("az")
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(PYTHON, [PYTHON, TOOL], {**self.env, "COLUMNS": "120"})
        out = b""
        deadline = time.time() + 20
        while b"hop to which?" not in out and time.time() < deadline:
            if select.select([fd], [], [], 0.2)[0]:
                out += os.read(fd, 4096)
        self.assertIn(b"hop to which? [1-3", out)
        self.assertIn(b"EMAIL / PROVIDER", out)
        self.assertRegex(out, rb"3\.\s+az\s+azure, res-a")
        os.write(fd, b"3\n")
        while time.time() < deadline:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        os.waitpid(pid, 0)
        os.close(fd)
        self.assertEqual(self.text(self.ptr).strip(), "az")

    def test_only_providers_still_list_and_hop(self):
        shutil.rmtree(self.store)
        os.remove(os.path.join(self.codex_home, "auth.json"))
        self.profile("az")
        self.assertIn("provider", self.run_tool("list").stdout)
        self.run_tool("az")
        self.assertEqual(self.text(self.ptr).strip(), "az")


if __name__ == "__main__":
    unittest.main()
