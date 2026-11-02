#!/usr/bin/env python3
"""What does a running `codex` do when codexhop hops the login under it?

Five cases, each run against a real Codex binary:

  follow        A session makes a request as login A. codexhop hops to B. The
                same session makes its next request. Which login does it carry?
  renew-after   As above, but A's access token enters Codex's 5-minute renewal
                window between the two requests, and the server then refuses A.
                Does Codex renew A, pick up B, or stop?
  renew-during  Codex starts renewing A; the hop lands while the token server
                is still answering. Where do A's renewed tokens end up?
  same-workspace  As renew-after, but the second login is another person in
                A's own workspace. Does the session carry on as that person?
  run-home      `codex exec` runs once as A, then `codexhop run bob exec ...`
                runs it as B from a run home whose databases are symlinks. Which
                login do its requests carry, and do SQLite's -wal and -shm files
                land beside the real databases or in the run home?

In the three renewal cases codexhop first refuses the hop (an open session is
about to renew the live login); the script then repeats it with --yes, to see
what happens to someone who overrides that.

A fake server on 127.0.0.1 plays the model endpoint, the token endpoint and the
ChatGPT backend over HTTPS (Codex 0.160 and later accept no other scheme for the
backend), with a throwaway certificate authority that Codex is told to trust
through CODEX_CA_CERTIFICATE. It records the bearer token and workspace header of every
request. The model's first reply calls a tool that does not exist, which makes
Codex send a second request in the same session without anyone typing. Codex
runs with a throwaway HOME and CODEX_HOME holding two made-up logins, and the
hop is a real `codexhop` command. After the run the script reports which login
each request carried and what auth.json holds, and runs `codexhop doctor`.

    unshare -rn sh -c 'ip link set lo up && python3 session-follows-hop.py /path/to/codex [case ...]'

`unshare -rn` cuts the network, so nothing can leave the machine; without it the
script still only points Codex at its own local server.

Results, 2 November 2026, Linux: see docs/how-it-works.md, "What open sessions do".
"""

import base64
import fcntl  # noqa: F401 - imported so a platform without it fails here, not mid-run
import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CODEXHOP = os.path.join(HERE, "..", "..", "codexhop.py")
CODEX = os.path.abspath(sys.argv[1])
CASES = [c for c in sys.argv[2:] if not c.startswith("--")] or [
    "follow", "renew-after", "renew-during", "same-workspace", "run-home"]
AUTH = "https://api.openai.com/auth"


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def jwt(claims):
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.c2ln"


def make_login(label, email, user, ws, exp_in, plan="plus"):
    now = int(time.time())
    auth = {"chatgpt_user_id": user, "user_id": user, "chatgpt_account_id": ws, "chatgpt_plan_type": plan}
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": jwt({"email": email, "iat": now, "exp": now + 3600, AUTH: auth}),
            "access_token": jwt({"iat": now, "exp": now + exp_in, "label": label, AUTH: auth,
                                 "https://api.openai.com/profile": {"email": email}}),
            "refresh_token": f"rt-{label}",
            "account_id": ws,
        },
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(now)),
    }


def label_of(token):
    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("label", "?")
    except Exception:
        return "?" if token else "-"


def make_certs(d):
    """A throwaway CA and a certificate for 127.0.0.1 signed by it. Returns (ca, cert, key)."""
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)  # noqa: E731
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
        "-days", "2", "-subj", "/CN=codexhop experiment CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr",
        "-subj", "/CN=127.0.0.1")
    with open(os.path.join(d, "ext.cnf"), "w") as f:
        f.write("subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n"
                "keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    run("openssl", "x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "srv.pem", "-days", "2", "-extfile", "ext.cnf")
    return tuple(os.path.join(d, f) for f in ("ca.pem", "srv.pem", "srv.key"))


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class Case:
    def __init__(self, name):
        self.name = name
        self.tmp = tempfile.mkdtemp(prefix=f"codexhop-exp-{name}-")
        self.home = os.path.join(self.tmp, "home")
        self.codex_home = os.path.join(self.home, ".codex")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.codex_home)
        os.makedirs(self.work)
        self.ca, self.cert, self.key = make_certs(self.tmp)
        self.log = []
        self.model_calls = 0
        self.notes = []
        self.t0 = time.time()
        # A's access token: valid for days, or close to Codex's 5-minute renewal window.
        exp_a = {"follow": 10 * 86400, "renew-after": 300 + 20, "renew-during": 200,
                 "same-workspace": 300 + 20, "run-home": 10 * 86400}[name]
        self.a = make_login("A", "ann@example.com", "user-a", "ws-a", exp_a)
        self.a2 = make_login("A2", "ann@example.com", "user-a", "ws-a", 10 * 86400)
        self.b = make_login("B", "bob@example.com", "user-b", "ws-b", 10 * 86400, plan="pro")
        if name == "same-workspace":
            self.b = make_login("B", "bob@example.com", "user-b", "ws-a", 10 * 86400)
        self.a_exp = json.loads(base64.urlsafe_b64decode(
            self.a["tokens"]["access_token"].split(".")[1] + "==")).get("exp")

    def env(self, port):
        keep = {k: v for k, v in os.environ.items() if k in ("LANG", "TERM") or k.startswith("LC_")}
        # The Codex under test comes first, so `codexhop doctor` reports its version.
        path = os.path.dirname(CODEX) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")
        return {**keep, "PATH": path, "HOME": self.home, "CODEX_HOME": self.codex_home, "NO_COLOR": "1",
                "CODEX_REFRESH_TOKEN_URL_OVERRIDE": f"https://127.0.0.1:{port}/oauth/token",
                "CODEX_CA_CERTIFICATE": self.ca,
                "CODEXHOP_OFFLINE": "1"}

    def hop(self, port, to="bob"):
        for extra in ([], ["--yes"]):
            p = subprocess.run([sys.executable, CODEXHOP, to, *extra], env=self.env(port),
                               capture_output=True, text=True, timeout=30)
            said = " | ".join(ln for ln in (p.stdout + p.stderr).split("\n") if ln.strip())
            self.notes.append(f"{time.time() - self.t0:5.1f}s  codexhop {' '.join([to, *extra])}: "
                              f"exit {p.returncode}; {said}")
            if p.returncode == 0:
                return

    def snapshot(self, when):
        doc = json.load(open(os.path.join(self.codex_home, "auth.json")))
        t = doc.get("tokens") or {}
        self.notes.append(f"{time.time() - self.t0:5.1f}s  auth.json {when}: tokens of login "
                          f"{label_of(t.get('access_token', ''))}, account_id field {t.get('account_id')}")

    def write_config(self, port):
        with open(os.path.join(self.codex_home, "config.toml"), "w") as f:
            f.write(
                f'openai_base_url = "https://127.0.0.1:{port}/backend-api/codex"\n'
                f'chatgpt_base_url = "https://127.0.0.1:{port}/backend-api"\n'
                'cli_auth_credentials_store = "file"\n'
                'approval_policy = "never"\n'
                'sandbox_mode = "read-only"\n'
                'check_for_update_on_startup = false\n'
                f'[projects."{self.work}"]\ntrust_level = "trusted"\n'
            )


def serve(case):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def record(self, body):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
            case.log.append({"t": round(time.time() - case.t0, 1), "method": self.command,
                             "path": self.path.split("?")[0], "login": label_of(tok),
                             "workspace": self.headers.get("chatgpt-account-id") or "-",
                             "rt": (body or {}).get("refresh_token") if isinstance(body, dict) else None})

        def send(self, status, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.record(None)
            if self.path.split("?")[0].endswith("/accounts/check"):  # Codex 0.166 asks before any model call
                ws = self.headers.get("chatgpt-account-id") or "-"
                origin = f"https://127.0.0.1:{self.server.server_address[1]}"
                self.send(200, {"accounts": [{"id": ws, "workspace_backend_origin": origin,
                                              "account_routing_override": "NO_CONSTRAINT"}]})
                return
            self.send(404, {"detail": "not here"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            self.record(body)
            port = self.server.server_address[1]
            path = self.path.split("?")[0]
            if path.endswith("/oauth/token"):
                if case.name == "renew-during":
                    case.hop(port)  # the hop lands while Codex waits for this answer
                t = case.a2["tokens"]
                self.send(200, {"id_token": t["id_token"], "access_token": t["access_token"],
                                "refresh_token": t["refresh_token"]})
                return
            if path.endswith("/responses"):
                case.model_calls += 1
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                if case.model_calls == 1:
                    if case.name in ("renew-after", "same-workspace"):
                        left = case.a_exp - time.time()
                        case.notes.append(f"first request: A's token has {left:.0f}s left (window is 300s)")
                        case.hop(port)
                        while case.a_exp - time.time() > 296:  # cross into the renewal window
                            time.sleep(0.2)
                    elif case.name == "renew-during":
                        case.snapshot("once Codex saved its renewal")
                    elif case.name == "run-home":
                        pass
                    else:
                        case.hop(port)
                    events = [{"type": "response.created", "response": {"id": "r1"}},
                              {"type": "response.output_item.done",
                               "item": {"type": "function_call", "call_id": "c1", "name": "codexhop_probe",
                                        "arguments": "{}"}},
                              {"type": "response.completed", "response": {"id": "r1", "usage": {
                                  "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}]
                    self.send(200, sse(events), "text/event-stream")
                    return
                if case.name in ("renew-after", "same-workspace") and label_of(tok) == "A":
                    self.send(401, {"error": {"message": "token expired", "code": "token_expired"}})
                    return
                events = [{"type": "response.created", "response": {"id": "r2"}},
                          {"type": "response.output_item.done",
                           "item": {"type": "message", "role": "assistant", "id": "m2",
                                    "content": [{"type": "output_text", "text": "done"}]}},
                          {"type": "response.completed", "response": {"id": "r2", "usage": {
                              "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}]
                self.send(200, sse(events), "text/event-stream")
                return
            self.send(404, {"detail": "not here"})

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(case.cert, case.key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def run_case(name):
    case = Case(name)
    httpd = serve(case)
    port = httpd.server_address[1]
    case.write_config(port)
    env = case.env(port)
    # Save both logins with codexhop, A live, the way a user would have them.
    with open(os.path.join(case.codex_home, "auth.json"), "w") as f:
        json.dump(case.b, f)
    os.chmod(os.path.join(case.codex_home, "auth.json"), 0o600)
    subprocess.run([sys.executable, CODEXHOP, "save", "bob"], env=env, capture_output=True, check=True)
    with open(os.path.join(case.codex_home, "auth.json"), "w") as f:
        json.dump(case.a, f)
    subprocess.run([sys.executable, CODEXHOP, "save", "ann"], env=env, capture_output=True, check=True)

    version = subprocess.run([CODEX, "--version"], capture_output=True, text=True, env=env).stdout.strip()
    case.t0 = time.time()
    if name == "run-home":
        # One plain run first, so the real home has databases for the run home to link.
        subprocess.run([CODEX, "exec", "--skip-git-repo-check", "-C", case.work, "say hello"],
                       capture_output=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
        case.notes.append(f"{time.time() - case.t0:5.1f}s  plain codex exec done; now codexhop run bob exec")
    command = [CODEX, "exec", "--skip-git-repo-check", "-C", case.work, "say hello"]
    if name == "run-home":
        command = [sys.executable, CODEXHOP, "run", "bob", "exec", "--skip-git-repo-check", "-C", case.work,
                   "say hello"]
    try:
        p = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120,
                           stdin=subprocess.DEVNULL)
        exit_code, out = p.returncode, (p.stdout + "\n" + p.stderr)
    except subprocess.TimeoutExpired as e:
        exit_code, out = "timeout", ((e.stdout or b"").decode() + (e.stderr or b"").decode())
    httpd.shutdown()

    live = json.load(open(os.path.join(case.codex_home, "auth.json")))
    lt = live.get("tokens") or {}
    doctor = subprocess.run([sys.executable, CODEXHOP, "doctor"], env=env, capture_output=True, text=True)
    print(f"\n=== {name}  ({version})")
    for n in case.notes:
        print(f"  note   {n}")
    for r in case.log:
        extra = f" refresh_token={r['rt']}" if r["rt"] else ""
        print(f"  {r['t']:5.1f}s {r['method']:4} {r['path']:<40} login={r['login']:<3} "
              f"workspace={r['workspace']}{extra}")
    print(f"  codex exit {exit_code}")
    for line in out.splitlines():
        words = ("error", "sign in", "signed in", "refresh", "credentials", "done")
        if any(w in line.lower() for w in words):
            print(f"  codex> {line.strip()[:200]}")
    print(f"  auth.json after: tokens of login {label_of(lt.get('access_token', ''))}, "
          f"account_id field {lt.get('account_id')}, refresh_token {lt.get('refresh_token')}")
    print("  codexhop doctor:")
    for line in doctor.stdout.splitlines()[5:]:
        if line.strip():
            print(f"    {line}")
    if name == "run-home":
        rh = os.path.join(case.codex_home, "codexhop", "run", "bob")
        real = sorted(e for e in os.listdir(rh) if not os.path.islink(os.path.join(rh, e)))
        dbs = sorted(e for e in os.listdir(case.codex_home) if ".sqlite" in e)
        print(f"  run home, entries that are not links: {', '.join(real)}")
        print(f"  real home, database files: {', '.join(dbs)}")
    if name == "renew-during":
        fixed = subprocess.run([sys.executable, CODEXHOP, "doctor", "--fix"], env=env, capture_output=True,
                               text=True)
        print("  codexhop doctor --fix:")
        for line in fixed.stdout.splitlines()[5:]:
            if line.strip():
                print(f"    {line}")
        ann = json.load(open(os.path.join(case.codex_home, "codexhop", "accounts", "ann.json")))["tokens"]
        live = json.load(open(os.path.join(case.codex_home, "auth.json")))["tokens"]
        print(f"  after the fix: ann holds login {label_of(ann['access_token'])} ({ann['refresh_token']}); "
              f"auth.json holds {label_of(live['access_token'])}, account_id field {live['account_id']}")
    if "--keep" not in sys.argv:
        shutil.rmtree(case.tmp, ignore_errors=True)


if __name__ == "__main__":
    for c in CASES:
        run_case(c)
