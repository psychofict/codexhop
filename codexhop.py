#!/usr/bin/env python3
"""codexhop - hop OpenAI Codex between ChatGPT accounts, and on to Azure, Bedrock or another provider.

To switch accounts, run `chop` and pick one. That is the whole tool. (`chop`
is the short name the shell glue gives `codexhop`; both work.)

  chop                 show your accounts and providers, pick one to hop to
  chop <name>          hop straight to that one
  chop add <name>      log in as a new account and save it

A hop changes the login that new `codex` sessions start with. A session that is
already open keeps the login it started with until it next renews it, and then
stops with "signed in to another account". To work as two accounts at once,
give the second terminal its own account with `chop run <name>`.

Providers (Azure OpenAI, Amazon Bedrock, any Responses API endpoint):

  chop provider add <name> --azure <endpoint> --model <deployment>
  chop provider add <name> --bedrock [--region R] [--aws-profile P]
  chop provider add <name> --base-url <url> [--env-key VAR]
  chop provider [list]      the providers set up, and whether each key variable is set here
  chop provider set <name> --launcher <command>   start codex through a command (a vault)
  chop provider rm <name>   delete a profile that codexhop wrote
  chop <provider>           hop to it; hop back with `chop <account>`
  chop run <provider> [..]  codex on it in this terminal only

A provider is a Codex profile file, ~/.codex/<name>.config.toml, so one you
wrote yourself works too. A hop to a provider reaches plain `codex` through the
shell glue: eval "$(codexhop shell-init --wrap-codex)". Your ChatGPT login is
left as it was.

Now and then:

  chop whoami          who is logged in right now (read from the login, no network)
  chop usage [name]    5-hour and weekly usage per account (asks the server, remembers the answer)
  chop next [names]    hop to another login with 5-hour room; names set the list once
  chop run <name> [..] codex as that account in this terminal only; the rest goes to codex
  chop renew [name]    renew the saved logins that are due (or that one, due or not)
  chop save [name]     save the login you have now, under a name
  chop list --long     token expiry, last renewal and when each account was saved
  chop rm <name>       delete a saved account (does not log you out)
  chop rename <old> <new>
  chop doctor [--fix]  check the setup, repair what it can
  chop shell-init      shell glue: eval "$(codexhop shell-init)"

Flags:

  -y, --yes            never prompt; also overrides the open-session stops
  --long               more columns in the listing
  --json               machine-readable output (never a token)
  --no-cache, --fresh  ask the server for usage even if the last reading is under a minute old
  --fix                doctor: repair what it can
  --alias NAME         shell-init: the short command's name (default chop)
  --wrap-codex         shell-init: also define `codex` so it follows a hop to a provider
  --no-color           plain text
  -V, --version        print the version

Also accepted: `use`/`switch` (what `chop <name>` does), `ls`, `active` (print
the active name), `sync` (write the live login back to its saved copy) and
`add <name> --device-auth`, which passes the option on to `codex login`.

How it works: Codex keeps its login in $CODEX_HOME/auth.json (~/.codex/auth.json).
codexhop keeps one copy of that file per account in
$CODEX_HOME/codexhop/accounts/<name>.json and swaps the live one in and out,
holding Codex's own auth.lock while it writes. Which account a login belongs to
is read from the login itself (the email, user and workspace in its ID token),
so listing, hopping and `whoami` need no network.

Environment: CODEX_HOME (default ~/.codex), CODEXHOP_DIR (default
$CODEX_HOME/codexhop), CODEXHOP_OFFLINE=1 to never use the network,
CODEXHOP_TIMEOUT, CODEXHOP_LOCK_WAIT and CODEXHOP_USAGE_URL. The reference
explains each one: https://github.com/psychofict/codexhop/blob/main/docs/commands.md
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse

VERSION = "1.0.0"
PROG = "codexhop"
DEFAULT_ALIAS = "chop"


def env(*names: str) -> str | None:
    """First of these environment variables that is set and non-empty."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _read_marker(home: str) -> dict:
    try:
        with open(os.path.join(home, RUN_MARKER)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# A `codexhop run` session points CODEX_HOME at a home of its own, and anything
# Codex runs inherits that. The marker file in that home names the real one, so
# `codexhop` typed inside such a session still acts on your real setup.
RUN_MARKER = ".codexhop-run"
HOME = os.path.expanduser("~")
_START_HOME = env("CODEX_HOME") or os.path.join(HOME, ".codex")
_MARKER = _read_marker(_START_HOME)
CODEX_HOME = _MARKER.get("home") or _START_HOME
AUTH_FILE = os.path.join(CODEX_HOME, "auth.json")
CONFIG_FILE = os.path.join(CODEX_HOME, "config.toml")
STORE = env("CODEXHOP_DIR") or _MARKER.get("store") or os.path.join(CODEX_HOME, "codexhop")
ACCOUNTS = os.path.join(STORE, "accounts")
ACTIVE_PTR = os.path.join(STORE, "active")
LOCK_PATH = os.path.join(STORE, ".lock")
RUN_DIR = os.path.join(STORE, "run")
USAGE_CACHE = os.path.join(STORE, "usage.json")
NEXT_LIST = os.path.join(STORE, "next")
PROVIDER_PTR = os.path.join(STORE, "provider")
PROVIDER_SETTINGS = os.path.join(STORE, "providers.json")

# Codex's own public OAuth client and token endpoint, and the variables Codex
# itself reads to override them. Renewing a saved login needs both.
CLIENT_ID = env("CODEX_APP_SERVER_LOGIN_CLIENT_ID") or "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = env("CODEX_REFRESH_TOKEN_URL_OVERRIDE") or "https://auth.openai.com/oauth/token"
CHATGPT_BASE_URL = "https://chatgpt.com/backend-api"
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"

API_TIMEOUT = float(env("CODEXHOP_TIMEOUT") or 10.0)
OFFLINE = bool(env("CODEXHOP_OFFLINE"))
LOCK_WAIT_S = float(env("CODEXHOP_LOCK_WAIT") or 10.0)

# The first Codex release that refuses to save a renewed login over a different
# one, and the first that takes auth.lock around its writes.
CODEX_CHECKS_RENEWAL = (0, 164, 0)
CODEX_LOCKS_AUTH = (0, 166, 0)

TMP_MARKER = ".tmp-codexhop"
USAGE_EXIT = 2

_ANSI = re.compile(r"\033\[[0-9;]*m")


def _use_color() -> bool:
    if "--no-color" in sys.argv or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE"):
        return True
    return sys.stdout.isatty()


BOLD, DIM, GREEN, YELLOW, RED, OFF = (
    ("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")
    if _use_color()
    else ("", "", "", "", "", "")
)


def die(msg: str, code: int = 1):
    print(f"{RED}error:{OFF} {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str):
    print(msg, file=sys.stderr)


def visible_len(s: str) -> int:
    """Width of a string once the colour escapes are stripped."""
    return len(_ANSI.sub("", s))


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - visible_len(s))


# ------------------------------------------------------------------ json files


def read_json(path: str) -> dict:
    """A JSON file, {} if it is missing. Stops the command if it will not parse.

    Codex rewrites auth.json in place, so a read that lands mid-write can see
    half a file. One retry a moment later tells that apart from a broken file.
    """
    for attempt in (1, 2):
        try:
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, RecursionError) as e:
            if attempt == 2:
                die(f"{path} is not valid JSON ({e}). Refusing to touch it.")
            time.sleep(0.2)
    return {}


def peek_json(path: str) -> dict:
    """read_json for code that only looks: a file that will not parse reads as empty."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_secure(path: str, data: dict):
    """Atomic, 0600, fsynced. A symlinked file is written where the link points."""
    path = os.path.realpath(path)
    tmp = f"{path}{TMP_MARKER}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def write_text_secure(path: str, text: str):
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# -------------------------------------------------------------- Codex's config
#
# Two settings matter here, both plain top-level strings: where Codex keeps the
# login (`cli_auth_credentials_store`) and which ChatGPT backend it talks to
# (`chatgpt_base_url`). Python 3.11 reads TOML itself; 3.9 and 3.10 do not, so
# those fall back to reading the lines above the first [table].


def config_value(key: str, path: str | None = None) -> str | None:
    path = path or CONFIG_FILE
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        import tomllib  # Python 3.11+

        value = tomllib.loads(raw.decode()).get(key)
        return value if isinstance(value, str) else None
    except ImportError:
        pass
    except (ValueError, UnicodeDecodeError):
        return None
    for line in raw.decode(errors="replace").splitlines():
        s = line.strip()
        if s.startswith("["):
            break
        m = re.match(rf"^{re.escape(key)}\s*=\s*([\"'])(.*?)\1\s*(#.*)?$", s)
        if m:
            return m.group(2)
    return None


def store_mode() -> str:
    """file, keyring, auto or ephemeral. Codex's default is the file."""
    return (config_value("cli_auth_credentials_store") or "file").lower()


def require_file_store():
    mode = store_mode()
    if mode != "file":
        die(
            f"Codex is set to keep its login in the {mode!r} store "
            f"(cli_auth_credentials_store in {CONFIG_FILE}). {PROG} works with the file store, "
            f"Codex's default. Set cli_auth_credentials_store = \"file\" there and run `codex login` "
            f"again; `{PROG} doctor` explains more."
        )


def usage_url() -> str:
    """Where Codex reads an account's rate limits from."""
    override = env("CODEXHOP_USAGE_URL")
    if override:
        return override
    base = (config_value("chatgpt_base_url") or CHATGPT_BASE_URL).rstrip("/")
    if base.startswith(("https://chatgpt.com", "https://chat.openai.com")) and "/backend-api" not in base:
        base += "/backend-api"
    return base + ("/wham/usage" if "/backend-api" in base else "/api/codex/usage")


# ------------------------------------------------------------ logins and claims
#
# A ChatGPT login is a signed ID token, an access token and a refresh token.
# Both JWTs name the email, the ChatGPT user and the workspace (Codex calls the
# workspace the account) in their claims, so which saved account a login is can
# be read straight out of it. Signatures are not checked: this only has to tell
# your own saved logins apart, never to trust a stranger's.
#
# A login is keyed on (user, workspace). One person can be in several
# workspaces and one workspace holds many people, so neither alone names a
# login, and the bare `account_id` field next to the tokens is never trusted on
# its own: Codex before 0.164 could save one login's renewed tokens under
# another login's account_id (see file_login).


def jwt_claims(token: str) -> dict:
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg))
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_time(value) -> float | None:
    """An RFC 3339 time (Codex writes nanoseconds and a Z) as an epoch second."""
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime

    # Python 3.9's fromisoformat takes only 3 or 6 fractional digits and no `Z`.
    iso = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value.replace("Z", "+00:00"))
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return when.timestamp() if when.tzinfo else None


def rfc3339_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def identity(doc: dict) -> dict:
    """Who a login is and how fresh, read from the login itself. No network.

    {"kind", "email", "plan", "user", "account", "accountField", "key",
    "issuedAt", "expiresAt", "lastRefresh"}; {} for an empty file. `key` is
    what two copies of one login share, None when nothing identifies it.
    """
    if not isinstance(doc, dict) or not doc:
        return {}
    tokens = doc.get("tokens") if isinstance(doc.get("tokens"), dict) else None
    api_key = doc.get("OPENAI_API_KEY")
    mode = doc.get("auth_mode")
    if tokens and mode != "apikey" and (tokens.get("id_token") or tokens.get("access_token")):
        idc = jwt_claims(tokens.get("id_token") or "")
        acc = jwt_claims(tokens.get("access_token") or "")
        a1 = idc.get(AUTH_CLAIM) if isinstance(idc.get(AUTH_CLAIM), dict) else {}
        a2 = acc.get(AUTH_CLAIM) if isinstance(acc.get(AUTH_CLAIM), dict) else {}
        user = (a1.get("chatgpt_user_id") or a1.get("user_id")
                or a2.get("chatgpt_user_id") or a2.get("user_id"))
        account = a1.get("chatgpt_account_id") or a2.get("chatgpt_account_id")
        profile = acc.get(PROFILE_CLAIM) if isinstance(acc.get(PROFILE_CLAIM), dict) else {}
        exp, iat = acc.get("exp"), acc.get("iat")
        return {
            "kind": "chatgpt",
            "email": idc.get("email") or profile.get("email"),
            "plan": a1.get("chatgpt_plan_type") or a2.get("chatgpt_plan_type"),
            "user": user,
            "account": account,
            "accountField": tokens.get("account_id"),
            "key": f"chatgpt:{user}:{account}" if user and account else None,
            "issuedAt": iat if isinstance(iat, (int, float)) else None,
            "expiresAt": exp if isinstance(exp, (int, float)) else None,
            "lastRefresh": parse_time(doc.get("last_refresh")),
        }
    if isinstance(api_key, str) and api_key:
        digest = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        return {"kind": "apikey", "email": None, "plan": "API key", "key": f"apikey:{digest}",
                "keyEnd": api_key[-4:], "issuedAt": None, "expiresAt": None, "lastRefresh": None}
    return {"kind": str(mode or "unknown"), "email": None, "plan": None, "key": None,
            "issuedAt": None, "expiresAt": None, "lastRefresh": None}


def mixed(ident: dict) -> bool:
    """A file whose account_id field names a different workspace than its tokens."""
    field = ident.get("accountField")
    return bool(ident.get("account") and field and ident["account"] != field)


def normalised(doc: dict) -> dict:
    """The login with its account_id field taken from its own tokens."""
    ident = identity(doc)
    if not mixed(ident):
        return doc
    fixed = json.loads(json.dumps(doc))
    fixed["tokens"]["account_id"] = ident["account"]
    return fixed


def issued(doc: dict) -> float:
    """When this copy of a login was issued: its access token's iat, else its last renewal."""
    ident = identity(doc)
    return ident.get("issuedAt") or ident.get("lastRefresh") or 0


def newer(a: dict | None, b: dict | None) -> bool:
    """True if copy a was issued after copy b."""
    return issued(a or {}) > issued(b or {})


def usable(doc: dict) -> bool:
    ident = identity(doc)
    if ident.get("kind") == "chatgpt":
        tokens = doc.get("tokens") or {}
        return bool(tokens.get("refresh_token") or tokens.get("access_token"))
    return ident.get("kind") == "apikey"


def who(ident: dict) -> str:
    if ident.get("kind") == "apikey":
        return f"API key ...{ident.get('keyEnd', '')}"
    return ident.get("email") or "email unknown"


def humanise(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def expired(doc: dict, margin: float = 0) -> bool:
    exp = identity(doc).get("expiresAt")
    return bool(exp) and exp - margin <= time.time()


def expiry_note(doc: dict) -> str:
    exp = identity(doc).get("expiresAt")
    if not exp:
        return "-"
    left = exp - time.time()
    if left <= 0:
        return f"{DIM}expired (renews on use){OFF}"
    return f"{humanise(left)} left"


def renewed_note(doc: dict) -> str:
    when = identity(doc).get("lastRefresh")
    return f"{humanise(time.time() - when)} ago" if when else "-"


# Codex renews a login on its first request once the access token is within 5
# minutes of running out. Only an access token whose expiry it cannot read is
# renewed by age instead, once the last renewal is 8 days old (both from Codex's
# source, 0.160.1 and 0.166.0). Access tokens last 240 hours: measured on a real
# token, 2 Nov 2026, iat to exp. `renew` does the logins no Codex is holding a
# day ahead of that, so a saved login never reaches its expiry unrenewed.
RENEW_SOON_S = 86400
RENEW_AGE_S = 8 * 86400
CODEX_WINDOW_S = 300


def codex_renews_at(doc: dict) -> float | None:
    """When a Codex that holds this login will renew it: on its first request
    after this time. None for a login Codex never renews."""
    ident = identity(doc)
    if ident.get("kind") != "chatgpt":
        return None
    if ident.get("expiresAt"):
        return ident["expiresAt"] - CODEX_WINDOW_S
    if ident.get("lastRefresh"):
        return ident["lastRefresh"] + RENEW_AGE_S
    return None


def renewal_due(doc: dict) -> bool:
    """Due for `renew`: the access token runs out within a day, or, for one
    whose expiry cannot be read, the last renewal is 8 days old."""
    ident = identity(doc)
    if ident.get("kind") != "chatgpt":
        return False
    exp, last = ident.get("expiresAt"), ident.get("lastRefresh")
    if exp:
        return exp - time.time() < RENEW_SOON_S
    return bool(last) and time.time() - last > RENEW_AGE_S


# ---------------------------------------------------------------------- locks
#
# Two `codexhop` runs at once could interleave read-modify-write on the same
# files and lose an account. Cheap to prevent.

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only in practice
    fcntl = None


class _Lock:
    """codexhop's own lock. The holder writes who it is into the lock file
    (pid, start time, command), so a command left waiting can say what it is
    waiting for instead of hanging silently."""

    def __init__(self, path: str):
        self.path = path
        self.fd = None

    def holder(self) -> str:
        try:
            with open(self.path) as f:
                pid, started, cmd = f.read().split(" ", 2)
            since = time.strftime("%H:%M", time.localtime(float(started)))
            return f" (pid {int(pid)}, `{cmd.strip()}`, since {since})"
        except (OSError, ValueError):
            return ""

    def __enter__(self):
        if fcntl is None:
            return self
        store_dir()
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            info(f"{DIM}waiting for another {PROG} to finish{self.holder()}...{OFF}")
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        me = f"{os.getpid()} {time.time():.0f} {' '.join([PROG] + sys.argv[1:])}"
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, me.encode(), 0)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


def lock():
    return _Lock(LOCK_PATH)


# ------------------------------------------------------------ Codex's auth.lock
#
# Since 0.166.0 Codex takes an exclusive flock on $CODEX_HOME/auth.lock for every
# read-modify-write of auth.json: loading, merging a renewal and saving. Holding
# the same lock while a hop reads the live login and writes the next one means a
# renewal Codex has already received either lands before the hop (and is saved
# to the right account) or finds the new login and is refused. Codex never holds
# the lock across the network call itself, so it is only ever held briefly, and
# older Codex ignores it. Keep the held section to file reads and writes.


class codex_auth_lock:  # noqa: N801 - used like a function: `with codex_auth_lock():`
    def __init__(self, home: str | None = None, required: bool = True):
        self.path = os.path.join(home or CODEX_HOME, "auth.lock")
        self.required = required
        self.fd = None

    def __enter__(self):
        if fcntl is None:
            return self
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                pass
            if time.monotonic() > deadline:
                os.close(self.fd)
                self.fd = None
                if not self.required:
                    return self
                die("Codex is writing its login right now, so nothing was changed. "
                    "Try again in a few seconds.")
            time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


# ------------------------------------------------------------- saved accounts


def store_dir() -> str:
    for d in (STORE, ACCOUNTS):
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.stat(d).st_mode & 0o077:
            os.chmod(d, 0o700)
    return ACCOUNTS


def account_path(name: str) -> str:
    return os.path.join(ACCOUNTS, f"{name}.json")


VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def check_name(name: str) -> str:
    if not VALID_NAME.match(name) or name in (".", ".."):
        die(
            f"'{name}' is not a usable account name. Use letters, digits, dot, "
            f"dash or underscore, starting with a letter or digit."
        )
    return name


def check_new_name(name: str) -> str:
    """check_name, plus: a name we are about to create must not hide a command.

    An account called `list` would be unreachable as `chop list`. Names that
    already exist are left alone.
    """
    check_name(name)
    if os.path.exists(account_path(name)):
        return name
    if name in list_providers():
        die(f"'{name}' is already a provider ({profile_path(name)}). Pick another name.")
    if name in COMMANDS:
        die(f"'{name}' is a {PROG} command, so `{PROG} {name}` could never hop to it. Pick another name.")
    return name


def list_accounts() -> list[str]:
    try:
        files = os.listdir(ACCOUNTS)
    except OSError:
        return []
    return sorted(f[:-5] for f in files if f.endswith(".json") and not f.startswith("."))


def load_account(name: str) -> dict:
    path = account_path(name)
    if not os.path.exists(path):
        known = ", ".join(list_accounts()) or "none saved yet"
        die(f"no saved account called '{name}'. Saved: {known}")
    return read_json(path)


def peek_account(name: str) -> dict:
    """An account for code that only looks: one that will not parse reads as empty."""
    return peek_json(account_path(name))


def save_account(name: str, doc: dict):
    store_dir()
    write_json_secure(account_path(name), doc)


def saved_at(name: str) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(os.stat(account_path(name)).st_mtime))
    except OSError:
        return ""


def read_ptr() -> str | None:
    try:
        with open(ACTIVE_PTR) as f:
            ptr = f.read().strip()
    except OSError:
        return None
    return ptr or None


def set_active_ptr(name: str):
    store_dir()
    write_text_secure(ACTIVE_PTR, name + "\n")


def clear_ptr():
    with contextlib.suppress(OSError):
        os.remove(ACTIVE_PTR)


def read_live() -> dict:
    return read_json(AUTH_FILE)


def write_live(doc: dict):
    """Put a login in auth.json. Call with codex_auth_lock() held."""
    os.makedirs(CODEX_HOME, mode=0o700, exist_ok=True)
    write_json_secure(AUTH_FILE, doc)


def accounts_for(key: str | None) -> list[str]:
    """Saved accounts holding a copy of the login with this key, pointer's first."""
    if not key:
        return []
    names = [n for n in list_accounts() if identity(peek_account(n)).get("key") == key]
    ptr = read_ptr()
    return sorted(names, key=lambda n: n != ptr)


def active_name() -> str | None:
    """Which saved account the live login is. No network: read from its claims."""
    live = peek_json(AUTH_FILE)
    key = identity(live).get("key")
    if key:
        names = accounts_for(key)
        return names[0] if names else None
    if live:  # a login with no identity (a mode codexhop does not read): exact copies only
        for n in list_accounts():
            if peek_account(n) == live:
                return n
    return None


def slug(ident: dict) -> str:
    if ident.get("kind") == "apikey":
        base = f"apikey-{ident.get('keyEnd', '')}".lower()
    else:
        email = ident.get("email") or ""
        base = email.split("@")[0].lower()
    s = re.sub(r"[^a-z0-9._-]", "-", base).strip("-.") or "account"
    if not VALID_NAME.match(s):
        s = "account-" + s.lstrip("-.")
    # An account called `list` would shadow the command of the same name.
    return f"{s}-acct" if s in COMMANDS else s


def free_name(base: str) -> str:
    names = set(list_accounts()) | set(list_providers())
    if base not in names:
        return base
    i = 2
    while f"{base}-{i}" in names:
        i += 1
    return f"{base}-{i}"


def file_login(doc: dict, prefer: str | None = None) -> str | None:
    """Save a login to the account it belongs to, by its claims. Returns the name.

    The newer copy wins, by when its access token was issued, so an older copy
    never overwrites a renewal. A login with no saved account yet is stashed
    under its email rather than dropped. A file whose account_id field names
    another workspace than its tokens is saved with the field put right: Codex
    before 0.164 wrote such files when a hop landed during its renewal, and the
    tokens, not the field, say whose login it is.
    """
    ident = identity(doc)
    if not ident:
        return None
    doc = normalised(doc)
    if ident.get("key"):
        names = accounts_for(ident["key"])
        if prefer in names:
            names.remove(prefer)
            names.insert(0, prefer)
    else:
        names = [n for n in list_accounts() if peek_account(n) == doc]
    if names:
        name = names[0]
        saved = peek_account(name)
        if saved != doc and not newer(saved, doc):
            save_account(name, doc)
        return name
    name = free_name(slug(ident))
    save_account(name, doc)
    info(f"{DIM}saved {who(ident)}, which had no saved account yet, as '{name}'{OFF}")
    return name


# ------------------------------------------------------------------ the network


def _http(url: str, method: str = "GET", headers: dict | None = None, body: dict | None = None,
          timeout: float = API_TIMEOUT) -> tuple[int, dict, dict]:
    """(status, JSON body, response headers). Status 0 when the server could not be reached."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": f"{PROG}/{VERSION}", "Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode(errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            return r.status, parsed if isinstance(parsed, dict) else {}, dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode(errors="replace"))
        except Exception:
            parsed = {}
        return e.code, parsed if isinstance(parsed, dict) else {}, dict(e.headers or {})
    except Exception as e:
        return 0, {"error": str(e)}, {}


# What the token endpoint's refusals mean, from Codex's own handling of them.
REFRESH_FAILURES = {
    "refresh_token_expired": "the login has expired",
    "refresh_token_reused": "this copy of the login was already renewed somewhere else",
    "refresh_token_invalidated": "the login was signed out or revoked",
    "invalid_grant": "the server no longer accepts this login",
}


def renew_login(doc: dict) -> tuple[dict | None, str]:
    """Trade the refresh token for fresh tokens. (renewed login, why not).

    The reply carries a new refresh token, and Codex treats the old one as spent
    (it has its own handling for a `refresh_token_reused` refusal). So whatever
    comes back MUST reach the disk before anything else happens; a copy left
    holding the old token may have to sign in again.
    """
    tokens = doc.get("tokens") if isinstance(doc.get("tokens"), dict) else {}
    rt = tokens.get("refresh_token")
    if not rt:
        return None, "no refresh token saved"
    status, body, _ = _http(
        TOKEN_URL, "POST",
        body={"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": rt},
    )
    if status != 200:
        err = body.get("error")
        code = err if isinstance(err, str) else (err or {}).get("code") if isinstance(err, dict) else None
        code = code or body.get("code")
        msg = body.get("error_description") or ((err or {}).get("message") if isinstance(err, dict) else None)
        if status == 0:
            return None, f"could not reach {TOKEN_URL} ({body.get('error', 'no answer')})"
        if code in REFRESH_FAILURES:
            return None, f"{REFRESH_FAILURES[code]} - `{PROG} add` it again"
        return None, f"http {status}" + (f": {msg or code}" if (msg or code) else "")
    if not body.get("access_token"):
        return None, "the token endpoint returned no access token"
    fresh = json.loads(json.dumps(doc))
    t = fresh.setdefault("tokens", {})
    for field in ("id_token", "access_token", "refresh_token"):
        if body.get(field):
            t[field] = body[field]
    acct = identity(fresh).get("account")
    if acct:
        t["account_id"] = acct
    fresh["last_refresh"] = rfc3339_now()
    return fresh, ""


def renew_and_file(name: str, doc: dict, live: bool = False) -> tuple[dict | None, str]:
    """Renew a saved login and write it to disk at once: the account, then auth.json.

    With live=True the caller holds codex_auth_lock(), and auth.json is read
    again under it: the copy renewed is the newest one there is, and the result
    goes back into auth.json only if that still holds this login.
    """
    in_live = False
    if live:
        current = read_live()
        in_live = bool(current) and identity(current).get("key") == identity(doc).get("key")
        if in_live and not newer(doc, current):
            doc = current
    fresh, err = renew_login(doc)
    if not fresh:
        return None, err
    save_account(name, fresh)  # the old refresh token is spent from here on
    if in_live:
        write_live(fresh)
    return fresh, ""


# ------------------------------------------------------------- running codex


def _cmdline(pid: str) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _ppid(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[1]
    except (OSError, IndexError):
        return ""


def _proc_home(pid: str) -> str | None:
    """The CODEX_HOME a process runs with, or None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env_block = f.read().split(b"\0")
    except OSError:
        return None
    vars_ = dict(e.decode(errors="replace").split("=", 1) for e in env_block if b"=" in e)
    home = vars_.get("CODEX_HOME") or os.path.join(vars_.get("HOME") or HOME, ".codex")
    marker = _read_marker(home)
    return marker.get("runHome") or home


def is_codex(argv: list[str]) -> bool:
    """A codex process: the native binary, or node running Codex's npm launcher."""
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base in ("codex", "codex.exe"):
        return True
    interpreters = ("node", "nodejs", "bun", "deno")
    return base in interpreters and len(argv) > 1 and os.path.basename(argv[1]) in ("codex", "codex.js")


def running_codex() -> list[tuple[int, str | None]]:
    """(pid, CODEX_HOME) for every codex process but this one. Best effort, never raises.

    The npm launcher starts the native binary as its child, so a child whose
    parent is also on the list is the same session and is left out.
    """
    me = str(os.getpid())
    if os.path.isdir("/proc"):
        try:
            entries = [e for e in os.listdir("/proc") if e.isdigit() and e != me]
        except OSError:
            entries = []
        found = {e for e in entries if is_codex(_cmdline(e))}
        return sorted((int(p), _proc_home(p)) for p in found if _ppid(p) not in found)
    if not shutil.which("pgrep"):  # macOS and anything else without /proc
        return []
    try:
        out = subprocess.run(["pgrep", "-x", "codex"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [(int(p), None) for p in out.split() if p.isdigit() and p != me]


def live_codex_pids() -> list[int]:
    """Codex processes using the live login: the real CODEX_HOME, or one we cannot see."""
    real = os.path.realpath(CODEX_HOME)
    return [pid for pid, home in running_codex() if home is None or os.path.realpath(home) == real]


def codex_version() -> tuple | None:
    codex = shutil.which("codex")
    if not codex:
        return None
    try:
        out = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(x) for x in m.groups()) if m else None


def vstr(v: tuple | None) -> str:
    return ".".join(map(str, v)) if v else "unknown"


def open_sessions_note(pids: list[int], was: str | None, old: dict, new: dict, name: str):
    """What happens to the sessions that were open, as measured on Codex 0.160.1
    and 0.166.0 (docs/how-it-works.md): each one keeps the login it started with
    until that login is due for renewal, then stops. Codex matches a renewal on
    the workspace alone, so when both logins are in one workspace the session
    takes up the new login at that point instead."""
    if not pids:
        return
    when = codex_renews_at(old)
    left = when - time.time() if when else None
    soon = "" if left is None else f", in about {countdown(left)}" if left >= 60 else ", any moment now"
    held = f"the '{was}' login" if was else "the login they started with"
    if identity(old).get("account") and identity(old).get("account") == identity(new).get("account"):
        then = f"then carry on as '{name}', which is in the same workspace"
    else:
        then = ("then stop with \"signed in to another account\". Restart them to move them over "
                "(`codex resume` picks a conversation back up)")
    info(
        f"{YELLOW}note:{OFF} {len(pids)} codex session(s) open (pid {', '.join(map(str, pids))}) keep using "
        f"{held} until it is next due for renewal{soon}, {then}."
    )


def env_overrides() -> list[str]:
    return [v for v in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN") if os.environ.get(v)]


class _TerminalGone(Exception):
    """SIGHUP/SIGTERM arrived - unwind so the cleanup blocks get to run."""


def _guard_child_signals() -> dict:
    """Make a long interactive child safe to wait on. Returns what to restore.

    * SIGINT. Codex uses Ctrl-C to interrupt its turn, so the first one must not
      unwind this process while `codex` is still running and still writing its
      login. A shell ignores SIGINT while a foreground child runs; do the same,
      with a no-op handler rather than SIG_IGN, which survives exec and would
      leave `codex` itself deaf to Ctrl-C.
    * SIGHUP/SIGTERM. The default action kills us outright, which skips every
      cleanup path and drops a login that has already been issued. Turn them
      into an exception instead.
    """

    def ignore(signum, frame):
        pass

    def bail(signum, frame):
        raise _TerminalGone(signum)

    old: dict = {}
    for sig, handler in (
        (getattr(signal, "SIGINT", None), ignore),
        (getattr(signal, "SIGHUP", None), bail),
        (getattr(signal, "SIGTERM", None), bail),
    ):
        if sig is None:
            continue
        try:
            old[sig] = signal.signal(sig, handler)
        except (OSError, ValueError):  # not the main thread, or unsupported here
            pass
    return old


def _restore_signals(old: dict):
    for sig, handler in old.items():
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            pass


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes or not sys.stdin.isatty():
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ------------------------------------------------------------------ run sessions
#
# `chop run <name>` starts one `codex` on its own account without touching the
# live login, so two terminals can be on two accounts at once.
#
# Each account gets a Codex home of its own, <store>/run/<name>/, and `codex` is
# pointed at it with CODEX_HOME. That home holds the account's auth.json; nearly
# everything else is a symlink to your real Codex home (config, AGENTS.md,
# skills, rules, sessions, history, the SQLite state), so a run session looks and
# remembers like your normal one, and `codex resume` sees the same threads. Codex
# writes config.toml through a symlink (it resolves the link, then renames), and
# SQLite puts its -wal and -shm files beside the real database, so links stay
# links. Left out are the login itself and what Codex keeps per login: the model
# list (it differs by plan), the cloud config cache (bound to a user and
# workspace) and the app-server daemon, which would carry the main login.
#
# The run home's login is the one that session renews, so it is the newest copy
# while the session lives; it is saved back to the account when the session
# ends, and `doctor --fix` collects it after a crash.

NEVER_LINKED = {
    "auth.json", "auth.lock", "models_cache.json", "cloud-config-bundle-cache.json",
    "app-server-control", "app-server-daemon", RUN_MARKER,
}


def run_home(name: str) -> str:
    return os.path.join(RUN_DIR, name)


def run_auth(name: str) -> str:
    return os.path.join(run_home(name), "auth.json")


def _proc_start(pid: int) -> str:
    """The start time /proc records for a pid; it tells a reused pid from ours."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, just not ours to signal
    return True


def run_pids(name: str) -> list[int]:
    """Open `run` sessions for this account. Entries for dead ones are dropped."""
    d = os.path.join(run_home(name), ".codexhop-pids")
    try:
        entries = os.listdir(d)
    except OSError:
        return []
    alive = []
    for e in entries:
        if not e.isdigit():
            continue
        pid = int(e)
        try:
            with open(os.path.join(d, e)) as f:
                started = f.read().strip()
        except OSError:
            continue
        if (_proc_start(pid) == started) if started else _pid_alive(pid):
            alive.append(pid)
        else:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, e))
    return sorted(alive)


def _register_pid(name: str, pid: int):
    d = os.path.join(run_home(name), ".codexhop-pids")
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, str(pid)), "w") as f:
        f.write(_proc_start(pid))


def _unregister_pid(name: str, pid: int):
    with contextlib.suppress(OSError):
        os.remove(os.path.join(run_home(name), ".codexhop-pids", str(pid)))


def run_login(name: str) -> dict:
    return peek_json(run_auth(name))


def _link_shared(home: str):
    store_real = os.path.realpath(STORE)
    try:
        entries = os.listdir(CODEX_HOME)
    except OSError:
        entries = []
    for entry in entries:
        src, dst = os.path.join(CODEX_HOME, entry), os.path.join(home, entry)
        if entry in NEVER_LINKED or entry.startswith("auth.json") or os.path.realpath(src) == store_real:
            continue
        if os.path.islink(dst):
            if os.readlink(dst) == src:
                continue
            os.remove(dst)
        elif os.path.lexists(dst):
            continue  # a real file or folder in the run home; not ours to replace
        os.symlink(src, dst)
    for entry in os.listdir(home):  # links whose target has gone
        dst = os.path.join(home, entry)
        if os.path.islink(dst) and not os.path.exists(dst):
            os.remove(dst)


def prepare_run_home(name: str, doc: dict) -> str:
    """Make <store>/run/<name>/ ready for a session. Call with lock() held."""
    home = run_home(name)
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    if os.stat(home).st_mode & 0o077:
        os.chmod(home, 0o700)
    write_text_secure(
        os.path.join(home, RUN_MARKER),
        json.dumps({"name": name, "home": CODEX_HOME, "store": STORE, "runHome": home}) + "\n",
    )
    if not run_pids(name):  # with a session open its login is the live one: hands off
        with codex_auth_lock(home):
            have = run_login(name)
            same = identity(have).get("key") == identity(doc).get("key")
            if not have or not same or newer(doc, have):
                write_json_secure(run_auth(name), doc)
            elif newer(have, doc):  # the last session renewed it: bring the account up to date
                save_account(name, normalised(have))
    _link_shared(home)
    return home


def capture_run_login(name: str) -> bool:
    """Save the login in the run home back to the account. True if that changed it.

    A `codex login` inside the session could have signed in as somebody else.
    That login is filed under its own account and the run home goes back to
    this one's.
    """
    home = run_home(name)
    if not os.path.isfile(run_auth(name)):
        return False
    with codex_auth_lock(home, required=False):
        new = run_login(name)
        old = peek_account(name)
        if not new or new == old:
            return False
        if identity(new).get("key") != identity(old).get("key"):
            other = file_login(new)
            if old:
                write_json_secure(run_auth(name), old)
            info(
                f"{YELLOW}note:{OFF} you signed in as {who(identity(new))} inside the '{name}' session. "
                f"Saved that login as '{other}'; '{name}' is unchanged."
            )
            return True
        if not newer(new, old):
            return False  # `renew` renewed the account since: the run home's copy is the stale one
        save_account(name, normalised(new))
        return True


def remove_run_home(name: str):
    """Delete a run home. Symlinks are unlinked, never followed: they point at
    your real Codex home."""
    home = run_home(name)
    if not os.path.isdir(home):
        return
    for root, dirs, files in os.walk(home, topdown=False, followlinks=False):
        for f in files:
            os.remove(os.path.join(root, f))
        for d in dirs:
            p = os.path.join(root, d)
            if os.path.islink(p):
                os.remove(p)
            else:
                os.rmdir(p)
    os.rmdir(home)


def effective_logins(names: list[str], cur: str | None, live: dict) -> dict[str, dict]:
    """The newest copy of each account's login: its saved file, the live login
    if it is that account, or its run home's login if a session renewed it."""
    out = {}
    for n in names:
        doc = peek_account(n)
        key = identity(doc).get("key")
        for other in ((live if n == cur else None), run_login(n)):
            if other and identity(other).get("key") == key and newer(other, doc):
                doc = other
        out[n] = doc
    return out


def cmd_run(name=None, rest=None, **_):
    """Run `codex` as one account in this terminal only."""
    rest = list(rest or [])
    if not name or name.startswith("-"):
        die(f"usage: {PROG} run <name> [codex arguments]", USAGE_EXIT)
    if not os.path.exists(account_path(name)) and name in list_providers():
        run_provider(name, rest)
    require_file_store()
    codex = shutil.which("codex")
    if not codex:
        die("`codex` is not on your PATH.")
    for var in env_overrides():
        info(f"{YELLOW}warning:{OFF} ${var} is set in this shell and overrides the saved login.")

    with lock():
        doc = load_account(name)
        if not usable(doc):
            die(f"'{name}' has no saved login. Save it again with `{PROG} add {name}`.")
        if active_name() == name:
            info(f"{DIM}'{name}' is the live login, so this is plain `codex`.{OFF}")
            os.execv(codex, [codex, *rest])
        home = prepare_run_home(name, doc)
        ident = identity(run_login(name) or doc)
        info(f"{DIM}codex as {name} ({who(ident)}), Codex home {home}{OFF}")
        old_signals = _guard_child_signals()
        try:
            proc = subprocess.Popen([codex, *rest], env={**os.environ, "CODEX_HOME": home})
        except OSError as e:
            _restore_signals(old_signals)
            die(f"could not start codex: {e}")
        _register_pid(name, proc.pid)

    code = 1
    try:
        try:
            code = proc.wait()
        except _TerminalGone:
            try:
                code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                code = 1
    finally:
        try:
            _unregister_pid(name, proc.pid)
            with lock():
                if capture_run_login(name):
                    info(f"{DIM}saved the renewed login for {name}{OFF}")
        finally:
            _restore_signals(old_signals)
    sys.exit(128 - code if code < 0 else code)


# ------------------------------------------------------------------ providers
#
# A provider is somewhere other than a ChatGPT login for Codex to get its model
# from: Azure OpenAI, Amazon Bedrock, or any endpoint that speaks the Responses
# API. Codex sets one up with a profile file, $CODEX_HOME/<name>.config.toml,
# that `codex --profile <name>` layers over config.toml. Codex 0.166 no longer
# reads a default `profile = "<name>"` from config.toml, so a hop to a provider
# cannot change a file Codex reads when it starts. codexhop keeps a pointer
# instead (<store>/provider), and the `codex` function that
# `shell-init --wrap-codex` defines reads it: with a provider hopped to it runs
# `codex --profile <name>`, otherwise plain `codex`.
#
# The ChatGPT login in auth.json is left alone, so hopping back is instant and
# its renewals go on as before. No key passes through codexhop: Codex reads an
# Azure key from the variable the profile names (or a launcher you set puts it
# there), and Bedrock uses the AWS credentials Codex finds on its own.

PROFILE_SUFFIX = ".config.toml"
MADE_BY = "# Written by codexhop"
WRAP_VAR = "CODEXHOP_WRAP"  # set by the shell glue's `codex` function
BUILTIN_PROVIDERS = {"openai", "amazon-bedrock", "ollama", "lmstudio", "gcp"}

# `codex --profile` is taken by the interactive screen and these commands. The
# others (login, update, doctor, ...) have no profile, so they run as plain codex.
PROFILE_COMMANDS = {"exec", "e", "review", "resume", "queue", "archive", "delete", "unarchive", "fork",
                    "mcp", "sandbox", "migrate-nfs-db"}
OTHER_COMMANDS = {"agents", "login", "logout", "plugin", "app-server", "remote-control", "completion",
                  "update", "doctor", "apply", "a", "migrate-rollouts", "cloud", "exec-server", "features",
                  "help"}


def profile_path(name: str) -> str:
    return os.path.join(CODEX_HOME, name + PROFILE_SUFFIX)


def table_value(path: str, table: str, key: str) -> str | None:
    """A string setting under [table] in a TOML file, or None. Python 3.9 and 3.10
    have no TOML reader, so those read the lines under the table's heading."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        import tomllib  # Python 3.11+

        node = tomllib.loads(raw.decode())
        for part in table.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        value = node.get(key) if isinstance(node, dict) else None
        return value if isinstance(value, str) else None
    except ImportError:
        pass
    except (ValueError, UnicodeDecodeError):
        return None
    inside = False
    for line in raw.decode(errors="replace").splitlines():
        s = line.strip()
        if s.startswith("["):
            inside = s.split("#", 1)[0].strip() == f"[{table}]"
        elif inside:
            m = re.match(rf"^{re.escape(key)}\s*=\s*([\"'])(.*?)\1\s*(#.*)?$", s)
            if m:
                return m.group(2)
    return None


def list_providers() -> list[str]:
    """Profile files that point Codex somewhere other than OpenAI's own service."""
    try:
        files = os.listdir(CODEX_HOME)
    except OSError:
        return []
    out = []
    for f in files:
        name = f[: -len(PROFILE_SUFFIX)]
        if not f.endswith(PROFILE_SUFFIX) or not VALID_NAME.match(name):
            continue
        path = profile_path(name)
        if os.path.isfile(path) and config_value("model_provider", path) not in (None, "openai"):
            out.append(name)
    return sorted(out)


def provider_settings() -> dict:
    return peek_json(PROVIDER_SETTINGS)


def set_provider_setting(name: str, key: str, value: str | None):
    data = read_json(PROVIDER_SETTINGS)  # stops if it will not parse, rather than dropping every launcher
    entry = dict(data[name]) if isinstance(data.get(name), dict) else {}
    if value:
        entry[key] = value
    else:
        entry.pop(key, None)
    if entry:
        data[name] = entry
    else:
        data.pop(name, None)
    store_dir()
    write_json_secure(PROVIDER_SETTINGS, data)


def read_provider_ptr(raw: bool = False) -> str | None:
    """The provider the hop points at. One whose profile file has gone reads as none, unless raw."""
    try:
        with open(PROVIDER_PTR) as f:
            name = f.read().strip()
    except OSError:
        return None
    if raw:
        return name or None
    return name if name and name in list_providers() else None


def set_provider_ptr(name: str):
    store_dir()
    write_text_secure(PROVIDER_PTR, name + "\n")


def clear_provider_ptr() -> str | None:
    """Drop the pointer. Returns the provider it named, if there was one."""
    was = read_provider_ptr()
    with contextlib.suppress(OSError):
        os.remove(PROVIDER_PTR)
    return was


def shown_url(url: str | None) -> str | None:
    """An endpoint as listed: without a user name, password, ?query or #fragment, which could hold a key."""
    if not url:
        return url
    try:
        u = urllib.parse.urlsplit(url)
        host = u.netloc.rsplit("@", 1)[-1]
        if not host:  # "https:////user:pw@host" has no host: the user name and password are in the path
            return None
        _ = u.port  # raises for a port that is not a number: that may be a password
        out = urllib.parse.urlunsplit((u.scheme, host, u.path, "", ""))
        urllib.parse.urlsplit(out)  # provider_where splits it again, so make sure that cannot fail
    except ValueError:  # e.g. "http://[bad": show nothing rather than guess what is safe
        return None
    return out


def env_name_problem(value: str) -> str | None:
    """Why this cannot be the name of the variable a key is read from, or None. Never repeats the value.

    Only the shape of the name can be checked. A real name is all upper case or all lower case, has
    a few digits at most, and is short. A key is mixed case, digit-heavy or long.
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        return "is not a variable name"
    mixed_case = value != value.upper() and value != value.lower()
    digits = sum(c.isdigit() for c in value)
    if mixed_case or digits > 4 or len(value) > 40 or (len(value) > 24 and "_" not in value):
        return "looks like a key, not the name of a variable"
    return None


def provider_details(name: str) -> dict:
    """What a provider's profile says, for listing. Never a key: there is none to read."""
    path = profile_path(name)
    pid = config_value("model_provider", path)
    table = f"model_providers.{pid}"
    env_key = table_value(path, table, "env_key")
    entry = provider_settings().get(name)
    launcher = entry.get("launcher") if isinstance(entry, dict) else None  # providers.json may be hand-edited
    return {
        "name": name, "provider": pid, "model": config_value("model", path), "file": path,
        "baseUrl": shown_url(table_value(path, table, "base_url")),
        "envKey": None if env_key and env_name_problem(env_key) else env_key,
        "region": table_value(path, table + ".aws", "region"),
        "awsProfile": table_value(path, table + ".aws", "profile"),
        "launcher": launcher if isinstance(launcher, str) and launcher.strip() else None,
    }


def provider_where(d: dict) -> str:
    if d["baseUrl"]:
        return urllib.parse.urlsplit(d["baseUrl"]).hostname or d["baseUrl"]  # shown_url already parsed it
    if d["region"]:
        return d["region"] + (f" ({d['awsProfile']})" if d["awsProfile"] else "")
    return ""


def provider_summary(d: dict) -> str:
    return ", ".join(x for x in (d["provider"], provider_where(d), d["model"]) if x)


def missing_key(d: dict) -> str | None:
    """The variable the profile reads its key from, when it is not set here and no launcher sets it."""
    var = d.get("envKey")
    return var if var and not d.get("launcher") and not os.environ.get(var) else None


def key_note(d: dict) -> str:
    if d["launcher"]:
        return f"launcher: {shown_launcher(d['launcher']) or '(unreadable)'}"
    if d["envKey"]:
        return f"${d['envKey']} {'set' if os.environ.get(d['envKey']) else 'not set'}"
    return "-"


def provider_json(name: str, ptr: str | None) -> dict:
    d = provider_details(name)
    out = {"name": name, "active": name == ptr, **{k: d[k] for k in (
        "provider", "model", "baseUrl", "region", "awsProfile", "envKey", "launcher", "file")}}
    out["launcher"] = shown_launcher(d["launcher"])
    return out


def has_profile_flag(args: list[str]) -> bool:
    return any(a in ("-p", "--profile") or a.startswith("--profile=") for a in args)


def takes_profile(args: list[str]) -> bool:
    """Whether `codex <args>` is something `--profile` applies to."""
    if any(a in ("-h", "--help", "-V", "--version") for a in args):
        return False
    for i, a in enumerate(args):
        if a == "--":
            break
        if a == "debug":  # only `debug prompt-input` reads a profile
            return args[i + 1:i + 2] == ["prompt-input"]
        if a in PROFILE_COMMANDS:
            return True
        if a in OTHER_COMMANDS:
            return False
    return True


def exec_provider(name: str, args: list[str]):
    """Replace this process with codex running on a provider. Does not return."""
    d = provider_details(name)
    var = missing_key(d)
    if var:
        info(f"{YELLOW}warning:{OFF} ${var} is not set in this shell, and '{name}' reads its key from it.")
    if d["launcher"]:
        try:
            argv = shlex.split(d["launcher"]) + args
        except ValueError as e:
            die(f"the launcher for '{name}' is not a command ({e}). "
                f"Fix it with `{PROG} provider set {name} --launcher ...`")
    else:
        codex = shutil.which("codex")
        if not codex:
            die("`codex` is not on your PATH.")
        argv = [codex, "--profile", name, *args]
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        die(f"could not start {argv[0]}: {e.strerror}")


def run_provider(name: str, rest: list[str]):
    if has_profile_flag(rest):
        die(f"'{PROG} run {name}' already picks the profile; drop --profile.", USAGE_EXIT)
    if not takes_profile(rest):
        die(f"that `codex` command does not use a profile, so it cannot run as '{name}'. "
            f"Run it as plain `codex`.", USAGE_EXIT)
    info(f"{DIM}codex on {name} ({provider_summary(provider_details(name))}){OFF}")
    exec_provider(name, rest)


def cmd_launch(rest=None, **_):
    """Start `codex` the way the hop says: on the provider hopped to, else as plain codex.

    This is what the `codex` function from `shell-init --wrap-codex` calls. A
    command with its own --profile, or one that takes no profile, runs untouched.
    """
    rest = list(rest or [])
    name = read_provider_ptr()
    if name and not has_profile_flag(rest) and takes_profile(rest):
        exec_provider(name, rest)
    codex = shutil.which("codex")
    if not codex:
        die("`codex` is not on your PATH.")
    os.execv(codex, [codex, *rest])


def hop_provider(name: str):
    d = provider_details(name)
    summary = provider_summary(d)
    if read_provider_ptr() == name:
        print(f"already on {GREEN}{name}{OFF} ({summary}, provider)")
        return
    set_provider_ptr(name)
    print(f"switched to {GREEN}{name}{OFF} ({summary}, provider)")
    var = missing_key(d)
    if var:
        info(f"{YELLOW}note:{OFF} ${var} is not set in this shell, and '{name}' reads its key from it.")
    if os.environ.get(WRAP_VAR):
        print(f"{DIM}new `codex` sessions start on this provider. Your ChatGPT login is left as it was.{OFF}")
    else:
        info(
            f"{YELLOW}note:{OFF} plain `codex` follows this hop only once your shell loads the glue with "
            f"--wrap-codex: add  eval \"$({PROG} shell-init --wrap-codex)\"  to your shell rc. "
            f"`{PROG} run {name}` works now."
        )


def provider_rows(ptr: str | None, names: list[str]) -> list[tuple]:
    return [("*" if n == ptr else " ", n, provider_summary(provider_details(n)), "provider") for n in names]


class QuietParser(argparse.ArgumentParser):
    """An argument parser whose complaint about extra arguments names flags and never values.

    Someone who types `--api-key sk-...` by mistake must not see the key repeated in the error.
    Abbreviations are off so an ambiguous `--a=VALUE` cannot be echoed either.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, allow_abbrev=False, **kw)

    def parse_args(self, args=None, namespace=None):
        ns, extra = self.parse_known_args(args, namespace)
        if extra:
            flags = [w.split("=", 1)[0] for w in extra if w.startswith("--")]
            others = len(extra) - sum(w.startswith("--") for w in extra)
            said = flags + ([f"{others} more value{'s' if others > 1 else ''}, not shown"] if others else [])
            self.error(f"unrecognized arguments: {', '.join(said)}")
        return ns


def _toml(value: str) -> str:
    return json.dumps(value)  # a JSON string is also a TOML basic string


def _endpoint(url: str, azure: bool) -> str:
    try:
        u = urllib.parse.urlsplit(url.strip())
        _ = u.port  # raises for a port that is not a number
    except ValueError:
        die("that is not an endpoint address: check the host and the port.", USAGE_EXIT)
    if "@" in u.netloc:
        die("the address has a user name or password in it. Keep the key in an environment variable, "
            "named with --env-key.", USAGE_EXIT)
    if u.scheme not in ("http", "https") or not u.netloc or u.query or u.fragment:
        die("that is not an endpoint address: use http or https, with no ?query part.", USAGE_EXIT)
    path = u.path.rstrip("/")
    if azure and not path:
        path = "/openai/v1"
    return f"{u.scheme}://{u.netloc}{path}"


def provider_add(argv: list[str]):
    p = QuietParser(
        prog=f"{PROG} provider add",
        description="Write a Codex profile for a provider, so `chop <name>` and `chop run <name>` "
                    "can use it.",
    )
    p.add_argument("name", help="what you will type to hop to it")
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--azure", metavar="ENDPOINT",
                      help="Azure OpenAI, e.g. https://<resource>.openai.azure.com "
                           "(the /openai/v1 part is added)")
    kind.add_argument("--bedrock", action="store_true", help="Amazon Bedrock")
    kind.add_argument("--base-url", metavar="URL", help="any other endpoint that speaks the Responses API")
    p.add_argument("--model", help="the model, or for Azure the deployment name (required for --azure)")
    p.add_argument("--env-key", metavar="VAR", help="the variable Codex reads the key from (Azure default: "
                                                    "AZURE_OPENAI_API_KEY)")
    p.add_argument("--region", help="--bedrock: the AWS region")
    p.add_argument("--aws-profile", metavar="PROFILE", help="--bedrock: the AWS profile to use")
    p.add_argument("--launcher", metavar="COMMAND",
                   help="a command that starts codex on this provider and gets codex's arguments, for when a "
                        "vault supplies the key")
    a = p.parse_args(argv)

    name = check_new_name(a.name)
    if os.path.exists(account_path(name)):
        die(f"'{name}' is a saved account. Pick another name.")
    path = profile_path(name)
    if os.path.lexists(path):
        die(f"{path} already exists. Edit it, or pick another name.")
    problem = env_name_problem(a.env_key) if a.env_key else None
    if problem:
        die(f"--env-key {problem}. Give the name of the variable that holds the key, never the key itself.",
            USAGE_EXIT)
    if a.launcher:
        check_launcher(a.launcher)
        read_json(PROVIDER_SETTINGS)  # stops now if providers.json is broken, before a profile is written

    lines = [f"{MADE_BY} ({PROG} provider add {name}). `codex --profile {name}` reads it; edit it freely."]
    if a.bedrock:
        if a.env_key:
            die("Amazon Bedrock takes its credentials from your AWS settings; --env-key does not apply.",
                USAGE_EXIT)
        lines.append('model_provider = "amazon-bedrock"')
    else:
        if a.region or a.aws_profile:
            die("--region and --aws-profile belong to --bedrock.", USAGE_EXIT)
        if a.azure and not a.model:
            die("--azure needs --model: Azure runs the deployment you name.", USAGE_EXIT)
        provider = "azure" if a.azure else re.sub(r"[^A-Za-z0-9_-]", "-", name)
        if provider in BUILTIN_PROVIDERS:
            die(f"'{name}' is the id of a provider built into Codex. Pick another name.", USAGE_EXIT)
        lines.append(f"model_provider = {_toml(provider)}")
    if a.model:
        lines.append(f"model = {_toml(a.model)}")
    lines.append("")
    if a.bedrock:
        aws = [f"{k} = {_toml(v)}" for k, v in (("region", a.region), ("profile", a.aws_profile)) if v]
        lines += (["[model_providers.amazon-bedrock.aws]", *aws] if aws else
                  ["# Region and credentials come from your AWS settings (AWS_PROFILE, AWS_REGION, ~/.aws)."])
    else:
        env_key = a.env_key or ("AZURE_OPENAI_API_KEY" if a.azure else None)
        lines += [
            f"[model_providers.{provider}]",
            f"name = {_toml('Azure OpenAI' if a.azure else name)}",
            f"base_url = {_toml(_endpoint(a.azure or a.base_url, bool(a.azure)))}",
        ]
        if env_key:
            lines.append(f"env_key = {_toml(env_key)}")
        lines.append('wire_api = "responses"')
    os.makedirs(CODEX_HOME, mode=0o700, exist_ok=True)
    write_text_secure(path, "\n".join(lines) + "\n")
    if a.launcher:
        set_provider_setting(name, "launcher", a.launcher)
    d = provider_details(name)
    print(f"added provider {GREEN}{name}{OFF} ({provider_summary(d)}), written to {path}")
    if d["envKey"] and not a.launcher:
        print(f"{DIM}Codex reads the key from ${d['envKey']}. {PROG} never stores it.{OFF}")
    print(f"{DIM}hop to it with `{PROG} {name}`, or run it in one terminal with `{PROG} run {name}`.{OFF}")


def _secret_assignment(word: str) -> str | None:
    """The variable name in a NAME=value word whose name says the value is a key, else None."""
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.+)", word, re.S)
    if not m or not re.search(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", m.group(1), re.I):
        return None
    if re.search(r"_(FILE|PATH)$", m.group(1), re.I):
        return None
    # A path is not a key: GOOGLE_APPLICATION_CREDENTIALS=/home/me/key.json, SSH_AUTH_SOCK=./sock.
    # Not for a name with KEY, TOKEN or SECRET in it: AWS_SECRET_ACCESS_KEY=/... is still refused.
    if m.group(2).startswith(("/", "~/", "./")) and not re.search(r"KEY|TOKEN|SECRET", m.group(1), re.I):
        return None
    return m.group(1)


def launcher_secrets(command: str) -> list[str]:
    try:
        return [n for n in map(_secret_assignment, shlex.split(command)) if n]
    except ValueError:
        return []


def shown_launcher(command: str | None) -> str | None:
    """A launcher as listed. A NAME=value word that looks like a key shows as NAME=***.

    One that is not a command at all shows as None: it cannot be checked, so it is not repeated.
    """
    if not command:
        return command
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not any(map(_secret_assignment, words)):
        return command
    return " ".join(w.split("=", 1)[0] + "=***" if _secret_assignment(w) else shlex.quote(w) for w in words)


def check_launcher(command: str):
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        die("that launcher is not a command.", USAGE_EXIT)
    names = launcher_secrets(command)
    if names:
        die(f"that launcher sets {', '.join(names)} to a value. A launcher is saved in {PROVIDER_SETTINGS} "
            f"and listed, so keep a key out of it: let the command read the key from your vault or keyring.",
            USAGE_EXIT)
    if not shutil.which(first):
        info(f"{YELLOW}note:{OFF} '{first}' is not on your PATH here.")


def provider_set(argv: list[str]):
    p = QuietParser(prog=f"{PROG} provider set", description="Change a provider's launcher.")
    p.add_argument("name")
    p.add_argument("--launcher", metavar="COMMAND", required=True,
                   help="the command that starts codex on this provider; empty ('') removes it")
    a = p.parse_args(argv)
    if a.name not in list_providers():
        die(f"no provider called '{a.name}'. Providers: {', '.join(list_providers()) or 'none yet'}")
    if a.launcher:
        check_launcher(a.launcher)
    set_provider_setting(a.name, "launcher", a.launcher or None)
    print(f"{a.name}: launcher {shown_launcher(a.launcher) or 'removed'}")


def provider_rm(name: str | None, yes: bool = False):
    if not name:
        die(f"usage: {PROG} provider rm <name>", USAGE_EXIT)
    if name not in list_providers():
        die(f"no provider called '{name}'. Providers: {', '.join(list_providers()) or 'none yet'}")
    path = profile_path(name)
    try:
        with open(path) as f:
            first = f.readline()
    except OSError as e:
        die(f"could not read {path}: {e.strerror}")
    if not first.startswith(MADE_BY):
        die(f"{path} was not written by {PROG}, so it is yours to delete; "
            f"`{PROG}` stops listing it once it is gone.")
    read_json(PROVIDER_SETTINGS)  # stops now if providers.json is broken, before anything is deleted
    if not confirm(f"delete provider '{name}' ({path})?", yes):
        die("aborted")
    os.remove(path)
    set_provider_setting(name, "launcher", None)
    if read_provider_ptr(raw=True) == name:
        clear_provider_ptr()
    print(f"deleted {name}")


def cmd_provider(args=None, **_):
    """chop provider [list | add | set | rm]"""
    args = list(args or [])
    sub, rest = (args[0], args[1:]) if args else ("list", [])
    yes = any(a in ("-y", "--yes") for a in rest)
    as_json = "--json" in rest
    rest = [a for a in rest if a not in ("-y", "--yes", "--json", "--no-color")]
    if sub in ("list", "ls"):
        names, ptr = list_providers(), read_provider_ptr()
        if as_json:
            print(json.dumps({"codexHome": CODEX_HOME, "provider": ptr,
                              "providers": [provider_json(n, ptr) for n in names]}, indent=2))
        elif not names:
            info("No providers yet.")
            info(f"Add one with:  {PROG} provider add <name> --azure <endpoint> --model <deployment>")
            info(f"          or:  {PROG} provider add <name> --bedrock --region <region>")
        else:
            rows = []
            for n in names:
                d = provider_details(n)
                rows.append(("*" if n == ptr else " ", n, d["provider"] or "?", provider_where(d) or "-",
                             d["model"] or "(Codex default)", key_note(d)))
            print_table(("", "NAME", "PROVIDER", "WHERE", "MODEL", "KEY"), rows,
                        {i for i, n in enumerate(names) if n == ptr})
        return
    if sub in ("add", "set", "rm", "remove", "delete"):
        with lock():
            if sub == "add":
                provider_add(rest)
            elif sub == "set":
                provider_set(rest)
            else:
                provider_rm(rest[0] if rest else None, yes)
        return
    die(f"unknown provider command '{sub}'. Try: {PROG} provider list | add | set | rm", USAGE_EXIT)


# ---------------------------------------------------------------------- usage
#
# How much of each account's 5-hour and weekly allowance is spent, from the
# endpoint Codex reads an account's rate limits from. It runs only when asked, keeps the last
# good reading on disk (percentages and times, never a token) and does not ask
# again before a server's Retry-After has passed.
#
# A reading needs a working access token. An idle account's token runs out 240
# hours after its last renewal, so usage renews it first, as `renew` would,
# unless a Codex has it open: the live login while codex is running, or an
# account open in a `run` session. Those are renewed by the Codex that holds them.

USAGE_FRESH_S = 60  # a reading this young is shown again without asking
USAGE_THROTTLE_DEFAULT_S = 600  # when a 429 names no Retry-After
USAGE_THROTTLE_MAX_S = 3600
SHORT_WINDOW_MAX_S = 86400  # a window this long or shorter goes in the 5-hour column


def api_usage(doc: dict) -> tuple[int, dict, float | None]:
    """(status, body, Retry-After in seconds if the server sent one)."""
    tokens = doc.get("tokens") or {}
    headers = {"Authorization": f"Bearer {tokens.get('access_token', '')}"}
    account = identity(doc).get("account")
    if account:
        headers["chatgpt-account-id"] = account
    status, body, resp_headers = _http(usage_url(), headers=headers)
    retry = None
    for k, v in resp_headers.items():
        if k.lower() == "retry-after":
            try:
                retry = float(v)
            except ValueError:
                pass
    return status, body, retry


def _window(win, now: float) -> dict | None:
    if not isinstance(win, dict):
        return None
    pct = win.get("used_percent")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    reset = win.get("reset_at")
    after = win.get("reset_after_seconds")
    if not isinstance(reset, (int, float)) and isinstance(after, (int, float)):
        reset = now + after
    length = win.get("limit_window_seconds")
    return {
        "pct": float(pct),
        "seconds": int(length) if isinstance(length, (int, float)) else None,
        "resetsAt": float(reset) if isinstance(reset, (int, float)) else None,
    }


def parse_usage(body: dict, now: float) -> dict:
    """The parts of a usage reply worth keeping, in a shape that is easy to show.

    {"plan", "short", "long", "limitReached", "extra": [{"name", "short", "long"}],
    "credits"}. A window is {"pct", "seconds", "resetsAt"}. The server names its
    windows primary and secondary; they go in the 5-hour or the weekly column
    by their length, so a plan that has only a weekly window shows it there.
    """

    def sort(rl: dict) -> tuple[dict | None, dict | None]:
        wins = [_window(rl.get("primary_window"), now), _window(rl.get("secondary_window"), now)]
        wins = [w for w in wins if w]
        short = next((w for w in wins if (w["seconds"] or 0) <= SHORT_WINDOW_MAX_S), None)
        long_ = next((w for w in wins if w is not short), None)
        return short, long_

    rl = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
    short, long_ = sort(rl)
    extra = []
    for row in body.get("additional_rate_limits") or []:
        if isinstance(row, dict) and isinstance(row.get("rate_limit"), dict):
            s, w = sort(row["rate_limit"])
            if s or w:
                extra.append({"name": str(row.get("limit_name") or row.get("metered_feature") or ""),
                              "short": s, "long": w})
    credits = body.get("credits") if isinstance(body.get("credits"), dict) else None
    return {
        "plan": body.get("plan_type") if isinstance(body.get("plan_type"), str) else None,
        "short": short,
        "long": long_,
        "limitReached": bool(rl.get("limit_reached")),
        "extra": extra,
        "credits": {k: credits.get(k) for k in ("has_credits", "unlimited", "balance")} if credits else None,
    }


def window_now(win: dict | None, now: float | None = None) -> dict | None:
    """A saved window as it stands now. Once its reset time has passed it is
    empty again, whatever it read when it was fetched; `was` keeps that figure."""
    if not win:
        return None
    now = now or time.time()
    if win.get("resetsAt") and win["resetsAt"] <= now:
        return {**win, "pct": 0.0, "resetsAt": None, "was": win.get("pct")}
    return dict(win)


def read_usage_cache() -> dict:
    return peek_json(USAGE_CACHE)


def renew_idle(name: str, doc: dict, cur: str | None, codex_live: list[int]) -> tuple[dict, str]:
    """Renew an account whose access token has run out, so its usage can be read.

    Returns (login to read with, what to say if it was not renewed). A login a
    Codex has open is left to that Codex: renewing it underneath would leave
    the session holding a retired refresh token.
    """
    if OFFLINE or identity(doc).get("kind") != "chatgpt" or not expired(doc):
        return doc, ""
    if run_pids(name):
        return doc, "the `run` session that has it open renews it"
    if name == cur and codex_live:
        return doc, "the open codex session renews it on its next request"
    if capture_run_login(name):  # a session that has ended may hold the newer login
        doc = peek_account(name) or doc
        if not expired(doc):
            return doc, ""
    with codex_auth_lock() if name == cur else contextlib.nullcontext():
        fresh, err = renew_and_file(name, doc, live=(name == cur))
    if not fresh:
        return doc, f"renewing it failed: {err}"
    return fresh, ""


def usage_row(entry: dict | None, doc: dict, now: float, hint: str = "", no_cache: bool = False) -> dict:
    """One account's reading: the cached entry, refreshed if it is allowed and due.

    Returns the entry to keep (usage, fetchedAt, throttledUntil) plus a `note`
    that says why a reading is old or missing.
    """
    entry = dict(entry or {})
    entry.pop("note", None)
    if OFFLINE:
        entry["note"] = "offline mode" if entry.get("usage") else "offline mode, nothing cached"
        return entry
    if identity(doc).get("kind") != "chatgpt":
        entry["note"] = "not a ChatGPT login; no usage limits to read"
        return entry
    fetched = entry.get("fetchedAt") or 0
    if not no_cache and entry.get("usage") is not None and now - fetched < USAGE_FRESH_S:
        return entry
    if expired(doc):
        entry["note"] = f"access token ran out; {hint}" if hint else "access token ran out"
        return entry
    until = entry.get("throttledUntil") or 0
    if until > now:
        entry["note"] = f"throttled by the server, ask again in {countdown(until - now)}"
        return entry
    status, body, retry = api_usage(doc)
    if status == 200:
        entry.update(usage=parse_usage(body, now), fetchedAt=now)
        entry.pop("throttledUntil", None)
        return entry
    if status == 429:
        wait = min(retry if retry is not None else USAGE_THROTTLE_DEFAULT_S, USAGE_THROTTLE_MAX_S)
        entry["throttledUntil"] = now + wait
        entry["note"] = f"throttled by the server, ask again in {countdown(wait)}"
    elif status in (401, 403):
        entry["note"] = f"the server refused this login (http {status})"
    else:
        entry["note"] = f"http {status}" if status else "could not reach the server"
    return entry


def read_usage(names: list[str], no_cache: bool = False) -> tuple[dict, str | None]:
    """Readings for these accounts, as fresh as the server allows, saved to the
    cache. Returns ({name: entry with its note}, the live account's name)."""
    cur = active_name()
    live = peek_json(AUTH_FILE)
    docs = effective_logins(names, cur, live)
    codex_live = live_codex_pids() if cur in names else []
    hints = {}
    for n in names:
        docs[n], hints[n] = renew_idle(n, docs[n], cur, codex_live)
        if not hints[n] and expired(docs[n]):
            hints[n] = f"`{PROG} renew {n}` renews it"
    cache = read_usage_cache()
    now = time.time()

    def one(n: str) -> dict:
        return usage_row(cache.get(n), docs[n], now, hints[n], no_cache)

    if len(names) > 1 and not OFFLINE:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(6, len(names))) as pool:
            rows = dict(zip(names, pool.map(one, names)))
    else:
        rows = {n: one(n) for n in names}
    for n, row in rows.items():
        cache[n] = {k: v for k, v in row.items() if k != "note"}
    keep = set(list_accounts())
    cache = {n: v for n, v in cache.items() if n in keep}
    with contextlib.suppress(OSError):
        store_dir()
        write_json_secure(USAGE_CACHE, cache)
    return rows, cur


def window_cell(win: dict | None) -> str:
    win = window_now(win)
    if not win:
        return "-"
    text = f"{win['pct']:.0f}%"
    secs = win.get("seconds")
    if secs and secs not in (18000, 604800):
        text += f" ({countdown(secs)} window)"
    if win.get("was") is not None:
        return f"{text}  reset, was {win['was']:.0f}%"
    left = (win.get("resetsAt") or 0) - time.time()
    return text + (f"  resets {countdown(left)}" if left > 0 else "")


def cmd_usage(name=None, as_json=False, no_cache=False, **_):
    """Where each account stands against its 5-hour and weekly limits."""
    names = list_accounts()
    if name:
        load_account(name)  # dies with the usual message if there is no such account
        names = [name]
    if not names:
        die(f"no saved accounts. Add one with `{PROG} add <name>`.")
    rows, cur = read_usage(names, no_cache=no_cache)
    now = time.time()

    if as_json:
        print(json.dumps({"accounts": [
            {"name": n, "active": n == cur, "usage": rows[n].get("usage"),
             "fetchedAt": rows[n].get("fetchedAt"), "throttledUntil": rows[n].get("throttledUntil"),
             "note": rows[n].get("note")}
            for n in names
        ]}, indent=2))
        return

    table, lit = [], set()
    for i, n in enumerate(names):
        row = rows[n]
        use = row.get("usage") or {}
        notes = [row["note"]] if row.get("note") else []
        if use.get("limitReached"):
            notes.insert(0, "limit reached")
        for x in use.get("extra") or []:
            cells = [c for c in (window_cell(x.get("short")), window_cell(x.get("long"))) if c != "-"]
            if cells:
                notes.append(f"{x['name'] or 'other'}: {' / '.join(cells)}")
        age = now - row["fetchedAt"] if use and row.get("fetchedAt") else 0
        if age > USAGE_FRESH_S:
            notes.append(f"last reading {countdown(age)} ago")
        table.append(("*" if n == cur else " ", n, window_cell(use.get("short")),
                      window_cell(use.get("long")), "; ".join(notes)))
        if n == cur:
            lit.add(i)
    print_table(("", "NAME", "5 HOURS", "WEEK", "NOTE"), table, lit)


# ----------------------------------------------------------------------- next
#
# `chop next` hops to whichever saved login has the most of its 5-hour window
# left. It is a command you run, never something that runs by itself: when to
# hop is your decision. Which logins it may choose from is a list you give it
# once (`chop next a b c`); it is kept in <store>/next and used from then on, so
# logins you keep for other work are never picked by accident.

NEXT_CURRENT_S = 300  # a reading older than this does not count as knowing
NEXT_FULL_PCT = 95.0  # a 5-hour window this full is not worth hopping into


def read_next_list() -> list[str]:
    try:
        with open(NEXT_LIST) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return []


def standing(entry: dict, now: float) -> dict:
    """What `next` knows about one login: {"known", "pct", "left", "full", "why"}."""
    fetched = entry.get("fetchedAt") or 0
    use = entry.get("usage")
    if use is None or now - fetched > NEXT_CURRENT_S:
        return {"known": False, "why": entry.get("note") or "no current reading"}
    short = window_now(use.get("short"), now) or {"pct": 0.0, "resetsAt": None}
    long_ = window_now(use.get("long"), now)
    pct = short["pct"]
    left = (short.get("resetsAt") or 0) - now
    left = left if left > 0 else None
    if long_ and long_["pct"] >= 100:
        wl = (long_.get("resetsAt") or 0) - now
        why = "weekly limit reached" + (f", resets {countdown(wl)}" if wl > 0 else "")
        return {"known": True, "pct": pct, "left": wl if wl > 0 else None, "full": True, "why": why}
    if pct >= NEXT_FULL_PCT or (use.get("limitReached") and not short.get("was")):
        why = f"5h {pct:.0f}%" + (f", resets {countdown(left)}" if left else "")
        return {"known": True, "pct": pct, "left": left, "full": True, "why": why}
    why = f"5h {pct:.0f}%" + (f", resets {countdown(left)}" if left else ", window not started")
    return {"known": True, "pct": pct, "left": left, "full": False, "why": why}


def cmd_next(names=None, yes=False, **_):
    """Hop to the login in the `next` list with the most 5-hour room left."""
    names = list(names or [])
    saved = list_accounts()
    if names:
        missing = [n for n in names if n not in saved]
        if missing:
            die(f"not saved: {', '.join(missing)}. Saved: {', '.join(saved) or 'none'}", USAGE_EXIT)
        names = list(dict.fromkeys(names))
        store_dir()
        write_text_secure(NEXT_LIST, "".join(n + "\n" for n in names))
        info(f"{DIM}`{PROG} next` now chooses among {', '.join(names)}{OFF}")
    else:
        names = [n for n in read_next_list() if n in saved]
        if not names:
            die(
                f"tell `{PROG} next` which logins it may choose from, once: "
                f"`{PROG} next <name> <name> ...`. Saved: {', '.join(saved) or 'none'}",
                USAGE_EXIT,
            )
    if len(names) < 2:
        die(f"`{PROG} next` needs at least two logins to choose between.", USAGE_EXIT)

    cur = active_name()
    others = [n for n in names if n != cur]
    rows, _ = read_usage(others)
    now = time.time()
    known = {n: standing(rows[n], now) for n in others}
    room = [n for n in others if known[n]["known"] and not known[n]["full"]]
    # Most room first. Within ten points, the window that resets soonest goes
    # first: what it has left is lost at the reset if nobody uses it.
    room.sort(key=lambda n: (int(known[n]["pct"] // 10), known[n]["left"] or float("inf")))

    width = max(len(n) for n in others)
    for n in others:
        mark = GREEN if room and n == room[0] else DIM
        info(f"  {mark}{pad(n, width)}{OFF}  {known[n]['why']}")
    if not room:
        resets = [(known[n]["left"], n) for n in others if known[n]["known"] and known[n]["left"]]
        unknown = [n for n in others if not known[n]["known"]]
        msg = "no login in the list has room"
        if resets:
            left, first = min(resets)
            msg += f"; {first} is the first to reset, in {countdown(left)}"
        if unknown:
            msg += f"; no current reading for {', '.join(unknown)}"
        die(msg)
    cmd_use(name=room[0], yes=yes)


# ------------------------------------------------------------------- commands


def print_table(hdr: tuple, rows: list, highlight=()):
    """Columns wide enough for their widest cell; highlight those row indexes."""
    w = [max(visible_len(str(r[i])) for r in [hdr, *rows]) for i in range(len(hdr))]
    print(DIM + "  ".join(pad(str(h), w[i]) for i, h in enumerate(hdr)).rstrip() + OFF)
    for i, r in enumerate(rows):
        line = "  ".join(pad(str(c), w[j]) for j, c in enumerate(r)).rstrip()
        print((GREEN + line + OFF) if i in highlight else line)


def account_json(n: str, doc: dict, cur: str | None) -> dict:
    """What --json says about one account. Identity and dates only, never a token."""
    ident = identity(doc)
    return {
        "name": n,
        "active": n == cur,
        "kind": ident.get("kind"),
        "email": ident.get("email"),
        "plan": ident.get("plan"),
        "userId": ident.get("user"),
        "workspaceId": ident.get("account"),
        "expiresAt": ident.get("expiresAt"),
        "lastRefresh": ident.get("lastRefresh"),
        "renewalDue": renewal_due(doc),
        "savedAt": saved_at(n) or None,
    }


def cmd_list(as_json=False, long_=False, **_):
    names = list_accounts()
    provs = [p for p in list_providers() if p not in names]
    ptr = read_provider_ptr()
    live = peek_json(AUTH_FILE)
    cur = active_name()
    if not names and not provs and not as_json:
        info("No accounts saved yet.")
        if live:
            info(f"You are logged in - save this one with:  {PROG} save <name>")
        else:
            info(f"Log in with:  {PROG} add <name>")
        return
    docs = effective_logins(names, cur, live)

    if as_json:
        print(json.dumps({"codexHome": CODEX_HOME, "active": cur, "provider": ptr,
                          "accounts": [account_json(n, docs[n], cur) for n in names],
                          "providers": [provider_json(n, ptr) for n in provs]}, indent=2))
        return

    idents = {n: identity(docs[n]) for n in names}
    emails = [i.get("email") for i in idents.values() if i.get("email")]
    # One email in two workspaces is two logins; only then is the workspace worth a column.
    show_ws = len(emails) != len(set(emails))
    hdr = ("", "NAME", "EMAIL / PROVIDER" if provs else "EMAIL", "PLAN") + (("WORKSPACE",) if show_ws else ())
    if long_:
        hdr += ("ACCESS", "RENEWED", "SAVED")
    rows = []
    for n in names:
        ident = idents[n]
        # While a provider is hopped to, new `codex` sessions use it: the star is on that, not on the login.
        star = "*" if n == cur and not ptr else " "
        row = (star, n, who(ident) if ident else "?", ident.get("plan") or "?")
        if show_ws:
            row += ((ident.get("account") or "?")[:8],)
        if long_:
            row += (expiry_note(docs[n]), renewed_note(docs[n]), saved_at(n))
        rows.append(row)
    lit = {i for i, n in enumerate(names) if n == cur and not ptr}
    for r in provider_rows(ptr, provs):
        if r[0] == "*":
            lit.add(len(rows))
        rows.append(r + ("",) * (len(hdr) - 4))
    print_table(hdr, rows, lit)

    due = [n for n in names if renewal_due(docs[n]) and n != cur]
    if due:
        info(f"{YELLOW}note:{OFF} due for renewal: {', '.join(due)}. `{PROG} renew` renews them.")
    if cur is None and live:
        info(
            f"\n{YELLOW}note:{OFF} the current login does not match any saved account. "
            f"Save it with:  {PROG} save <name>"
        )


def cmd_pick(yes=False, as_json=False, long_=False, **_):
    """`chop` with nothing after it: show the accounts and offer to hop.

    Falls back to a plain listing when there is nothing to choose between, or
    when there is nobody at a terminal to answer.
    """
    names = list_accounts()
    provs = [p for p in list_providers() if p not in names]
    if len(names) + len(provs) < 2 or not (sys.stdin.isatty() and sys.stdout.isatty()):
        cmd_list(as_json=as_json, long_=long_)
        return

    live = peek_json(AUTH_FILE)
    cur = active_name()
    ptr = read_provider_ptr()
    docs = effective_logins(names, cur, live)
    # Which one to pick is mostly a question of who has room left, so show the
    # last 5-hour reading `chop usage` saved. Read from the cache, never asked
    # for: the picker has to come up at once.
    cache = read_usage_cache()
    now = time.time()

    def last_5h(n: str) -> str:
        entry = cache.get(n) or {}
        use = entry.get("usage")
        if not use or not entry.get("fetchedAt"):
            return "-"
        win = window_now(use.get("short"), now) or {"pct": 0.0}
        if win.get("was") is not None:
            return "0% (reset)"
        cell, age = f"{win['pct']:.0f}%", now - entry["fetchedAt"]
        return cell if age < 900 else f"{cell} ({countdown(age)} ago)"

    show_5h = any((cache.get(n) or {}).get("usage") for n in names)
    choices = names + provs

    def menu_row(i: int, n: str) -> tuple:
        if n in provs:
            row = (f"{i}. *" if n == ptr else f"{i}.", n, provider_summary(provider_details(n)), "provider")
            return row + (("-",) if show_5h else ())
        star = n == cur and not ptr  # with a provider hopped to, it is the one new sessions use
        return (
            (f"{i}. *" if star else f"{i}.", n, who(identity(docs[n])), identity(docs[n]).get("plan") or "?")
            + ((last_5h(n),) if show_5h else ())
        )

    rows = [menu_row(i, n) for i, n in enumerate(choices, 1)]
    lit = {i for i, n in enumerate(choices) if (n == ptr if n in provs else n == cur and not ptr)}
    hdr = ("", "NAME", "EMAIL / PROVIDER" if provs else "EMAIL", "PLAN") + (("5H USED",) if show_5h else ())
    print_table(hdr, rows, lit)
    try:
        answer = input(f"\nhop to which? [1-{len(choices)}, Enter to stay] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not answer:
        return
    if answer.isdigit() and 1 <= int(answer) <= len(choices):
        chosen = choices[int(answer) - 1]
    else:  # a name, or enough of one to be unambiguous
        hits = [n for n in choices if n == answer] or [n for n in choices if n.startswith(answer)]
        if len(hits) != 1:
            die(f"'{answer}' is not one of them. Pick a number, or a name.", USAGE_EXIT)
        chosen = hits[0]

    # The menu was read without the lock, and a menu can stay open for an hour
    # while something else hops. Take the lock now and decide from what is live.
    with lock():
        if chosen == active_name() and not read_provider_ptr():
            print(f"already on {GREEN}{chosen}{OFF}")
            return
        cmd_use(name=chosen, yes=yes)


def cmd_active(as_json=False, **_):
    """The name new `codex` sessions start as: the provider hopped to, else the live login's account."""
    cur, ptr = active_name(), read_provider_ptr()
    if as_json:
        print(json.dumps({"active": cur, "provider": ptr, "codexHome": CODEX_HOME}))
    elif ptr or cur:
        print(ptr or cur)
    else:
        sys.exit(1)


def cmd_whoami(as_json=False, **_):
    live = peek_json(AUTH_FILE)
    ptr = read_provider_ptr()
    prov = f"{ptr} ({provider_summary(provider_details(ptr))})" if ptr else None
    if not live:
        if as_json:
            print(json.dumps({"loggedIn": False, "codexHome": CODEX_HOME, "provider": ptr}))
        elif prov:
            print(f"{BOLD}provider {prov}{OFF}  (no ChatGPT login)")
        else:
            print("not logged in")
        if not ptr:
            sys.exit(1)
        return
    ident = identity(live)
    name = active_name()
    overrides = env_overrides()
    if as_json:
        print(json.dumps({
            "loggedIn": True, "codexHome": CODEX_HOME, "account": name, "kind": ident.get("kind"),
            "email": ident.get("email"), "plan": ident.get("plan"), "userId": ident.get("user"),
            "workspaceId": ident.get("account"), "expiresAt": ident.get("expiresAt"),
            "lastRefresh": ident.get("lastRefresh"), "envOverrides": overrides, "provider": ptr,
        }, indent=2))
        return
    tail = f"  access: {expiry_note(live)}" if ident.get("kind") == "chatgpt" else ""
    print(f"{BOLD}{who(ident)}{OFF}  [{ident.get('plan') or '?'}]  saved as: {name or '(unsaved)'}{tail}")
    if prov:
        print(f"hopped to provider {BOLD}{prov}{OFF}: new `codex` sessions use it, not this login")
    for var in overrides:
        info(f"{YELLOW}warning:{OFF} ${var} is set in this shell and overrides the saved login.")


def cmd_save(name=None, yes=False, **_):
    require_file_store()
    live = read_live()
    if not live:
        die("not logged in - nothing to save. Run `codex login` first.")
    ident = identity(live)
    already = accounts_for(ident.get("key")) if ident.get("key") else []
    if not name:
        name = already[0] if already else slug(ident)
    check_new_name(name)
    if already and name not in already:
        die(f"this login is already saved as '{already[0]}'. Two copies would renew apart and one would "
            f"stop working; `{PROG} rename {already[0]} {name}` gives it the new name.")
    existing = peek_account(name)
    if existing and identity(existing).get("key") != ident.get("key") and not confirm(
            f"'{name}' holds {who(identity(existing))}; replace it with {who(ident)}?", yes):
        die("aborted")
    if existing and identity(existing).get("key") == ident.get("key") and newer(existing, live):
        print(f"{GREEN}{name}{OFF} already holds a newer copy of this login")
    else:
        save_account(name, normalised(live))
        print(f"saved {GREEN}{name}{OFF} ({who(ident)}) -> {account_path(name)}")
    set_active_ptr(name)


def cmd_sync(**_):
    live = read_live()
    if not live:
        die("not logged in - nothing to sync.")
    n = active_name()
    if not n:
        die(f"the current login is not one of the saved accounts. Use `{PROG} save <name>`.")
    with codex_auth_lock():
        file_login(read_live(), prefer=n)
    print(f"synced the live login into {n}")


def cmd_renew(name=None, yes=False, **_):
    """Renew saved logins: the ones that are due, or the one named, due or not.

    A saved login you have not hopped to in weeks has nobody renewing it, and a
    refresh token can expire (Codex has an answer for `refresh_token_expired`).
    This is the Codex that is not running, renewing a day ahead of when Codex would.
    """
    if OFFLINE:
        die(f"{PROG} is in offline mode ($CODEXHOP_OFFLINE); renewing needs the network.")
    names = list_accounts()
    if not names:
        die("no saved accounts to renew.")
    if name:
        load_account(name)  # dies with the usual message if it is not a real one
        names = [name]

    cur = active_name()
    codex_live = live_codex_pids() if cur in names else []
    failed = 0
    for n in names:
        pids = run_pids(n)
        if pids and not yes:
            print(f"  {DIM}{n}{OFF}  skipped - open in a run session, "
                  f"pid {', '.join(map(str, pids))} (--yes overrides)")
            continue
        if not pids:
            capture_run_login(n)  # a session that has ended may hold the newer login
        doc = effective_logins([n], cur, peek_json(AUTH_FILE))[n]
        if identity(doc).get("kind") != "chatgpt":
            kind = identity(doc).get("plan") or identity(doc).get("kind")
            print(f"  {DIM}{n}{OFF}  nothing to renew ({kind})")
            continue
        if not name and not renewal_due(doc):
            print(f"  {DIM}{n}{OFF}  not due (access {expiry_note(doc)}, renewed {renewed_note(doc)})")
            continue
        # Renewing retires the refresh token. A Codex that already holds the
        # live login keeps the old one in memory, so leave it to that Codex.
        if n == cur and codex_live and not yes:
            print(f"  {DIM}{n}{OFF}  skipped - the live login, with {len(codex_live)} codex session(s) "
                  f"open; they renew it themselves (--yes overrides)")
            continue
        # Only the live login is in Codex's way: hold its lock for the round
        # trip and read the login again under it.
        with codex_auth_lock() if n == cur else contextlib.nullcontext():
            fresh, err = renew_and_file(n, doc, live=(n == cur))
        if not fresh:
            print(f"  {RED}{n}{OFF}  {err}")
            failed += 1
            continue
        print(f"  {GREEN}{n}{OFF}  access {expiry_note(fresh)}")
    if failed:
        sys.exit(1)


def cmd_use(name=None, yes=False, **_):
    if not name:
        die(f"usage: {PROG} use <name>", USAGE_EXIT)
    if not os.path.exists(account_path(name)) and name in list_providers():
        hop_provider(name)
        return
    require_file_store()
    target = load_account(name)
    tid = identity(target)
    if not usable(target):
        die(f"'{name}' has no saved login. Save it again with `{PROG} add {name}`.")

    live = read_live()
    lid = identity(live)
    if live and (lid.get("key") == tid.get("key") if tid.get("key") else live == target):
        with codex_auth_lock():
            file_login(read_live(), prefer=name)
        set_active_ptr(name)
        left = clear_provider_ptr()
        if left:
            print(f"switched to {GREEN}{name}{OFF} ({who(tid)}, {tid.get('plan') or '?'}); "
                  f"left provider {left}")
        else:
            print(f"already on {GREEN}{name}{OFF} ({who(tid)})")
        return

    pids = run_pids(name)
    if pids and not yes:
        die(
            f"'{name}' is open in a `{PROG} run` session (pid {', '.join(map(str, pids))}). A second copy "
            f"of its login would renew on its own and one of the two would be signed out. Close that "
            f"session first, or pass --yes."
        )
    if not pids:
        capture_run_login(name)  # a session that has ended may hold the newer login
        target = load_account(name)

    # An open Codex renews its login on its first request inside the renewal
    # window. If the hop lands while that renewal is on the wire, the renewed
    # tokens never reach a file codexhop reads (measured: Codex 0.166 drops them,
    # 0.160 writes them under the new login's workspace) and the old refresh
    # token is already spent. Inside the window, wait for that request first.
    codex_live = live_codex_pids()
    due = codex_renews_at(live) if live else None
    if codex_live and due is not None and due - time.time() < 60 and not yes:
        die(
            f"the live login ({who(lid)}) is due for renewal and {len(codex_live)} codex session(s) have it "
            f"open (pid {', '.join(map(str, codex_live))}). The next request one of them makes renews it, "
            f"and a hop that lands during that renewal can cost the account its login. Hop once that "
            f"session has made its next request, or close it first, or pass --yes."
        )

    # Hand over a login that works as it lands. Codex would renew a spent access
    # token itself on its next request, but `whoami` and a statusline would
    # report a dead token until then, and a login that cannot be renewed any
    # more is better found now than in the middle of a task.
    if not OFFLINE and tid.get("kind") == "chatgpt" and expired(target, margin=300):
        fresh, err = renew_and_file(name, target)
        if fresh:
            target = fresh
        else:
            info(f"{YELLOW}note:{OFF} could not renew '{name}' first ({err}); hopping to the saved copy")

    version = codex_version() if codex_live else None
    was = active_name()
    # The network work is done. From here to the write we hold Codex's lock and
    # read the live login again: Codex may have renewed it since we first looked,
    # and the copy we file away must be that one.
    with codex_auth_lock():
        live = read_live()
        if live:
            was = file_login(live) or was
        write_live(target)
    set_active_ptr(name)
    left = clear_provider_ptr()
    print(f"switched to {GREEN}{name}{OFF} ({who(tid)}, {tid.get('plan') or '?'})"
          + (f"; left provider {left}" if left else ""))
    if codex_live and version and version < CODEX_CHECKS_RENEWAL:
        info(
            f"{YELLOW}note:{OFF} Codex {vstr(version)} can save an open session's renewal over the new "
            f"login. `{PROG} doctor` checks for that; Codex {vstr(CODEX_CHECKS_RENEWAL)} or later cannot."
        )
    open_sessions_note(codex_live, was, live, target, name)
    print(f"{DIM}new `codex` sessions start as this account.{OFF}")


def cmd_add(name=None, yes=False, rest=None, **_):
    """Sign in with `codex login` in a home of its own and save that login.

    The live login is never touched: Codex signs in inside a temporary home
    that keeps the login in a file, and the result is copied out of it.
    """
    if not name or name.startswith("-"):
        die(f"usage: {PROG} add <name> [codex login options]", USAGE_EXIT)
    check_new_name(name)
    require_file_store()
    if os.path.exists(account_path(name)) and not confirm(f"'{name}' already exists - replace it?", yes):
        die("aborted")
    codex = shutil.which("codex")
    if not codex:
        die("`codex` is not on your PATH, so I can't run the login. Install Codex first.")

    # No lock while you sign in, which can take minutes: the temporary home is
    # this command's alone. The lock is taken for the save at the end.
    store_dir()
    tmp = tempfile.mkdtemp(prefix=".add-", dir=STORE)
    with open(os.path.join(tmp, "config.toml"), "w") as f:
        f.write('cli_auth_credentials_store = "file"\n')
    info(f"\nSigning in for {BOLD}{name}{OFF} with `codex login`. Your current login stays as it is.")
    info(
        f"{DIM}If your browser is signed in to ChatGPT as another account, that account is what you will\n"
        f"get. Open the sign-in link in a private window, or pass --device-auth.{OFF}\n"
    )
    old_signals = _guard_child_signals()
    hung_up = False
    try:
        try:
            subprocess.call([codex, "login", *(rest or [])], env={**os.environ, "CODEX_HOME": tmp})
        except _TerminalGone:
            hung_up = True
        new = peek_json(os.path.join(tmp, "auth.json"))
    finally:
        _restore_signals(old_signals)
        shutil.rmtree(tmp, ignore_errors=True)

    if not new or not usable(new):
        info(f"{YELLOW}no new login found after that.{OFF} Nothing saved for '{name}'.")
        sys.exit(1)
    new = normalised(new)
    ident = identity(new)
    plan = ident.get("plan") or "?"
    with lock():
        elsewhere = [n for n in accounts_for(ident.get("key")) if n != name]
        if elsewhere:
            die(
                f"you signed in as {who(ident)}, which is saved as '{elsewhere[0]}'. Nothing saved for "
                f"'{name}'. Sign in as the other account (a private browser window helps), or "
                f"`{PROG} rename {elsewhere[0]} {name}`."
            )
        save_account(name, new)
        with codex_auth_lock():
            live = peek_json(AUTH_FILE)
            if not live:
                write_live(new)
                set_active_ptr(name)
                print(f"saved and now live: {GREEN}{name}{OFF} ({who(ident)}, {plan})")
            elif identity(live).get("key") == ident.get("key"):
                # A fresh sign-in for the login that is live, likely because that
                # one stopped renewing. Open sessions pick it up when they renew.
                write_live(new)
                print(f"saved {GREEN}{name}{OFF} ({who(ident)}, {plan}) and replaced the live copy of it")
            else:
                print(f"saved {GREEN}{name}{OFF} ({who(ident)}, {plan}). Hop to it with `{PROG} {name}`.")
    if hung_up:
        info(f"{DIM}(the terminal went away, but the login was saved){OFF}")


def cmd_rm(name=None, yes=False, **_):
    if not name:
        die(f"usage: {PROG} rm <name>", USAGE_EXIT)
    if not os.path.exists(account_path(name)) and name in list_providers():
        provider_rm(name, yes)
        return
    load_account(name)  # existence check
    pids = run_pids(name)
    if pids:
        die(f"'{name}' is open in a `{PROG} run` session (pid {', '.join(map(str, pids))}). Close it first.")
    if name == active_name():
        info(f"{YELLOW}note:{OFF} '{name}' is the login you are using right now; deleting the saved copy "
             f"does not log you out.")
    if not confirm(f"delete saved account '{name}'?", yes):
        die("aborted")
    os.remove(account_path(name))
    remove_run_home(name)  # it holds a copy of the login
    if read_ptr() == name:
        clear_ptr()
    print(f"deleted {name}")


def cmd_rename(name=None, new=None, **_):
    if not name or not new:
        die(f"usage: {PROG} rename <old> <new>", USAGE_EXIT)
    check_new_name(new)
    doc = load_account(name)
    if os.path.exists(account_path(new)):
        die(f"'{new}' already exists")
    pids = run_pids(name)
    if pids:
        die(f"'{name}' is open in a `{PROG} run` session (pid {', '.join(map(str, pids))}). Close it first.")
    save_account(new, doc)
    os.remove(account_path(name))
    if os.path.isdir(run_home(name)):
        os.rename(run_home(name), run_home(new))
    if read_ptr() == name:
        set_active_ptr(new)
    listed = read_next_list()
    if name in listed:
        write_text_secure(NEXT_LIST, "".join((new if n == name else n) + "\n" for n in listed))
    cache = read_usage_cache()
    if name in cache:
        cache[new] = cache.pop(name)
        with contextlib.suppress(OSError):
            write_json_secure(USAGE_CACHE, cache)
    print(f"renamed {name} -> {new}")


def cmd_doctor(fix=False, as_json=False, **_):
    problems: list[dict] = []

    def note(level: str, msg: str, fixed: str | None = None):
        problems.append({"level": level, "message": msg, "fixed": fixed})

    version = codex_version()
    if not shutil.which("codex"):
        note("warn", "`codex` is not on your PATH")
    elif version and version < CODEX_CHECKS_RENEWAL:
        note("warn", f"Codex {vstr(version)} does not check that auth.json still holds the same login when "
                     f"it saves a renewal, so a hop during a renewal can mix two logins in one file. "
                     f"{PROG} files every login by its tokens and this check repairs such a file; "
                     f"Codex {vstr(CODEX_CHECKS_RENEWAL)} or later closes the gap")

    mode = store_mode()
    if mode != "file":
        note("error", f"Codex keeps its login in the {mode!r} store (cli_auth_credentials_store in "
                      f"{CONFIG_FILE}). {PROG} reads and writes auth.json, so it needs the file store: set "
                      f"cli_auth_credentials_store = \"file\" and run `codex login` again")
    for var in env_overrides():
        note("warn", f"${var} is set and overrides the saved login entirely")

    for d in (STORE, ACCOUNTS):
        if os.path.isdir(d):
            m = os.stat(d).st_mode & 0o777
            if m & 0o077:
                if fix:
                    os.chmod(d, 0o700)
                    note("warn", f"{d} was {m:o}", "chmod 700")
                else:
                    note("error", f"{d} is mode {m:o}; it should be 700")
    files = [account_path(n) for n in list_accounts()] + ([AUTH_FILE] if os.path.exists(AUTH_FILE) else [])
    for p in files:
        m = os.stat(p).st_mode & 0o777
        if m & 0o077:
            if fix:
                os.chmod(p, 0o600)
                note("warn", f"{p} was {m:o}", "chmod 600")
            else:
                note("error", f"{p} is mode {m:o}; it should be 600")

    live = peek_json(AUTH_FILE)
    lid = identity(live)
    if live and mixed(lid):
        if fix:
            with codex_auth_lock():
                filed = file_login(read_live())
                ptr = read_ptr()
                back = peek_account(ptr) if ptr else {}
                if back and identity(back).get("key") != lid.get("key"):
                    write_live(back)
                    how = f"filed its tokens under '{filed}' and put '{ptr}' back"
                else:
                    write_live(normalised(read_live()))
                    how = f"filed its tokens under '{filed}'"
            note("warn", "auth.json held tokens of one workspace under another's account_id", how)
        else:
            note("error", f"auth.json holds tokens for {who(lid)} (workspace {lid['account'][:8]}) under the "
                          f"account_id of workspace {str(lid['accountField'])[:8]}, so Codex sends the wrong "
                          f"workspace; --fix files the tokens under their own account")

    names = list_accounts()
    seen: dict[str, str] = {}
    for n in names:
        doc = peek_account(n)
        ident = identity(doc)
        if not doc:
            note("error", f"{account_path(n)} is empty or not valid JSON; delete it and `{PROG} add {n}`")
            continue
        if not usable(doc):
            note("error", f"'{n}' has no login in it; `{PROG} add {n}` again")
        if mixed(ident):
            if fix:
                save_account(n, normalised(doc))
                note("warn", f"'{n}' had another workspace's account_id beside its tokens", "put right")
            else:
                note("error", f"'{n}' has another workspace's account_id beside its tokens; "
                              f"--fix puts it right")
        key = ident.get("key")
        if key and key in seen:
            note("warn", f"'{n}' and '{seen[key]}' are the same login. Each renewal retires the other "
                         f"copy's refresh token; keep one (`{PROG} rm {n}`)")
        elif key:
            seen[key] = n

    cur = active_name()
    docs = effective_logins(names, cur, live)
    overdue = [n for n in names if renewal_due(docs[n]) and n != cur]
    if overdue:
        note("warn", f"due for renewal: {', '.join(overdue)}; `{PROG} renew`")
    for n in names:
        saved = peek_account(n)
        if newer(docs[n], saved):
            where = "live login" if n == cur else "login in its run home"
            if fix:
                save_account(n, normalised(docs[n]))
                note("warn", f"'{n}' saved copy was behind the {where}", "synced")
            else:
                note("warn", f"'{n}' saved copy is behind the {where}; --fix saves it")

    ptr = read_ptr()
    if ptr and ptr not in names:
        if fix:
            if cur:
                set_active_ptr(cur)
            else:
                clear_ptr()
            note("warn", f"active pointer named '{ptr}', which is not a saved account", "reset")
        else:
            note("error", f"active pointer names '{ptr}', which is not a saved account; --fix resets it")
    provs = list_providers()
    pptr = read_provider_ptr(raw=True)
    if pptr and pptr not in provs:
        if fix:
            clear_provider_ptr()
            note("warn", f"the hop pointed at provider '{pptr}', whose profile file is gone", "reset")
        else:
            note("warn", f"the hop points at provider '{pptr}', whose profile file {profile_path(pptr)} is "
                         f"gone; --fix resets it")
    for n in provs:
        if n in names:
            note("warn", f"'{n}' is both a saved account and a provider profile; `{PROG} {n}` hops to the "
                         f"account. Rename one of them")
    for n in provs:
        raw = table_value(profile_path(n), f"model_providers.{provider_details(n)['provider']}", "env_key")
        problem = env_name_problem(raw) if raw else None
        if problem:
            note("warn", f"provider '{n}' has an env_key that {problem}, so {PROG} does not list it or check "
                         f"that it is set. If that value is a key, take it out of {profile_path(n)}")
    if pptr in provs:
        var = missing_key(provider_details(pptr))
        if var:
            note("warn", f"provider '{pptr}' reads its key from ${var}, which is not set in this shell")
        if not os.environ.get(WRAP_VAR):
            note("warn", f"the hop to provider '{pptr}' reaches plain `codex` only through the shell glue, "
                         f"and this shell has not loaded it: eval \"$({PROG} shell-init --wrap-codex)\"")
    if live and cur is None:
        note("warn", f"the live login ({who(lid)}) is not saved; `{PROG} save <name>`")
    if not live and not pptr:
        note("warn", "not logged in right now")

    if os.path.isdir(RUN_DIR):
        for d in sorted(os.listdir(RUN_DIR)):
            if d not in names:
                if fix:
                    remove_run_home(d)
                    note("warn", f"run home for '{d}', which is not a saved account", "removed")
                else:
                    note("warn", f"run home for '{d}', which is not a saved account (it holds a login); "
                                 f"--fix removes it")
    stale = []
    for d in (STORE, ACCOUNTS):
        if os.path.isdir(d):
            stale += [os.path.join(d, f) for f in os.listdir(d) if TMP_MARKER in f]
    if os.path.isdir(STORE):  # a sign-in can be under way in another terminal; an hour is not
        stale += [os.path.join(STORE, f) for f in os.listdir(STORE) if f.startswith(".add-")
                  and time.time() - os.stat(os.path.join(STORE, f)).st_mtime > 3600]
    for f in stale:
        if fix:
            if os.path.isdir(f):
                shutil.rmtree(f, ignore_errors=True)
            else:
                os.remove(f)
            note("warn", f"stale file {f}", "removed")
        else:
            note("warn", f"stale file {f} (it can hold a login); --fix removes it")

    pids = live_codex_pids()
    if pids:
        note("info", f"{len(pids)} codex session(s) open on the live login (pid {', '.join(map(str, pids))})")

    if as_json:
        print(json.dumps({
            "version": VERSION, "codexVersion": vstr(version) if version else None, "codexHome": CODEX_HOME,
            "store": STORE, "credentialsStore": mode, "accounts": names, "active": active_name(),
            "providers": provs, "provider": read_provider_ptr(), "problems": problems,
        }, indent=2))
    else:
        print(f"{BOLD}{PROG} {VERSION}{OFF}")
        print(f"  codex        {vstr(version) if version else 'not found'}")
        print(f"  codex home   {CODEX_HOME} (login kept in: {mode})")
        print(f"  store        {STORE} ({len(names)} saved)")
        print(f"  active       {active_name() or '(none)'}")
        if provs:
            print(f"  provider     {read_provider_ptr() or '(none hopped to)'} ({len(provs)} set up)")
        print()
        if not [p for p in problems if p["level"] != "info"]:
            print(f"{GREEN}no problems found{OFF}")
        for p in problems:
            colour = RED if p["level"] == "error" else YELLOW if p["level"] == "warn" else DIM
            suffix = f" {GREEN}[{p['fixed']}]{OFF}" if p["fixed"] else ""
            print(f"  {colour}{p['level']}{OFF}  {p['message']}{suffix}")
    if any(p["level"] == "error" and not p["fixed"] for p in problems):
        sys.exit(1)


def cmd_help(**_):
    print(__doc__.strip())


def cmd_version(**_):
    print(f"{PROG} {VERSION}")


# Second names for a command, kept working but not worth offering on TAB.
ALIASES = {"ls", "current", "switch", "login", "remove", "delete", "mv", "check", "refresh", "launch"}


def _completion_words() -> tuple[str, str]:
    verbs = " ".join(k for k in COMMANDS if not k.startswith("-") and k not in ALIASES)
    flags = " ".join(sorted(k for k in FLAGS if k.startswith("--")) + ["--alias"])
    return verbs, flags


def cmd_shell_init(alias=None, wrap_codex=False, **_):
    """Print shell glue: `eval "$(codexhop shell-init)"` in your shell rc.

    It adds the short command (`chop`, or whatever --alias names) and
    tab-completion for commands, flags, account and provider names. With
    --wrap-codex it also defines a `codex` function that follows a hop to a
    provider (see `launch`).
    """
    alias = alias or DEFAULT_ALIAS
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", alias):
        die(f"'{alias}' is not a usable command name.", USAGE_EXIT)
    verbs, flags = _completion_words()
    print(
        textwrap.dedent(
            f"""\
            # {PROG} {VERSION} - add to your shell rc:  eval "$({PROG} shell-init)"
            if [ -n "${{ZSH_VERSION:-}}" ]; then
              autoload -Uz +X bashcompinit 2>/dev/null && bashcompinit 2>/dev/null
            fi
            alias {alias}='{PROG}'
            _codexhop_names() {{
              local d="${{CODEXHOP_DIR:-${{CODEX_HOME:-$HOME/.codex}}/codexhop}}/accounts" f n
              for f in "$d"/*.json; do
                [ -e "$f" ] || continue
                n="${{f##*/}}"
                printf '%s ' "${{n%.json}}"
              done
              for f in "${{CODEX_HOME:-$HOME/.codex}}"/*.config.toml; do
                [ -e "$f" ] || continue
                n="${{f##*/}}"
                printf '%s ' "${{n%.config.toml}}"
              done
            }}
            _codexhop_complete() {{
              local cur prev names
              cur="${{COMP_WORDS[COMP_CWORD]}}"
              prev="${{COMP_WORDS[COMP_CWORD-1]}}"
              names="$(_codexhop_names)"
              if [ "$COMP_CWORD" -eq 1 ]; then
                COMPREPLY=($(compgen -W "{verbs} $names" -- "$cur"))
              else
                case "$prev" in
                  use|switch|rm|remove|delete|rename|mv|save|add|login|renew|refresh|usage|next|run)
                    COMPREPLY=($(compgen -W "$names" -- "$cur")) ;;
                  *)
                    COMPREPLY=($(compgen -W "{flags}" -- "$cur")) ;;
                esac
              fi
            }}
            complete -F _codexhop_complete {PROG} {alias} 2>/dev/null
            """
        ).rstrip()
    )
    if wrap_codex:
        print(
            textwrap.dedent(
                f"""\
                export {WRAP_VAR}=1
                codex() {{
                  if command -v {PROG} >/dev/null 2>&1; then
                    command {PROG} launch -- "$@"
                  else
                    command codex "$@"
                  fi
                }}
                """
            ).rstrip()
        )


COMMANDS = {
    "list": cmd_list, "ls": cmd_list,
    "use": cmd_use, "switch": cmd_use,
    "save": cmd_save,
    "add": cmd_add, "login": cmd_add,
    "whoami": cmd_whoami, "current": cmd_whoami,
    "active": cmd_active,
    "sync": cmd_sync,
    "renew": cmd_renew, "refresh": cmd_renew,
    "rm": cmd_rm, "remove": cmd_rm, "delete": cmd_rm,
    "rename": cmd_rename, "mv": cmd_rename,
    "usage": cmd_usage,
    "next": cmd_next,
    "run": cmd_run,
    "provider": cmd_provider,
    "launch": cmd_launch,
    "doctor": cmd_doctor, "check": cmd_doctor,
    "shell-init": cmd_shell_init,
    "help": cmd_help, "-h": cmd_help, "--help": cmd_help,
    "version": cmd_version, "-V": cmd_version, "--version": cmd_version,
}

# Commands that only read; they skip the lock and never write.
READ_ONLY = {cmd_list, cmd_whoami, cmd_active, cmd_help, cmd_version, cmd_shell_init}

FLAGS = {
    "-y": "yes", "--yes": "yes",
    "--long": "long_", "-l": "long_",
    "--json": "as_json",
    "--fix": "fix",
    "--no-color": "no_color",
    "--no-cache": "no_cache",
    "--fresh": "no_cache",
    "--wrap-codex": "wrap_codex",
}


def main(argv: list[str]):
    # `run` and `add` hand everything after the account name on to codex.
    if argv[:1] in (["run"], ["add"], ["login"]):
        rest = argv[2:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        name = argv[1] if len(argv) > 1 else None
        if argv[0] == "run":
            cmd_run(name=name, rest=rest)
        else:
            yes = any(a in ("-y", "--yes") for a in rest)
            rest = [a for a in rest if a not in ("-y", "--yes")]
            cmd_add(name=name, yes=yes, rest=rest)  # takes the lock itself, after the sign-in
        return
    # `launch` starts codex and `provider` has options of its own, so neither goes through the flag parser.
    if argv[:1] == ["launch"]:
        rest = argv[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        cmd_launch(rest=rest)
        return
    if argv[:1] == ["provider"]:
        cmd_provider(argv[1:])
        return

    flags = {"yes": False, "long_": False, "as_json": False, "fix": False, "no_color": False,
             "no_cache": False, "alias": None, "wrap_codex": False}
    args: list[str] = []
    only_positional = False
    it = iter(argv)
    for a in it:
        if only_positional:
            args.append(a)
        elif a == "--":
            only_positional = True
        elif a == "--alias" or a.startswith("--alias="):
            flags["alias"] = a.split("=", 1)[1] if "=" in a else next(it, None)
            if not flags["alias"]:
                die("--alias needs a name", USAGE_EXIT)
        elif a in FLAGS:
            flags[FLAGS[a]] = True
        elif a.startswith("-") and a not in COMMANDS:
            die(f"unknown option '{a.split('=', 1)[0]}'. Try: {PROG} help", USAGE_EXIT)  # never the value
        else:
            args.append(a)

    # Bare `chop` in a terminal asks which account you want; everywhere else -
    # a pipe, --json, a statusline - it stays a plain listing, and takes no lock.
    # The picker takes the lock itself, only once you have chosen: a menu left
    # open must not hold up every other codexhop command.
    if not args and not flags["as_json"]:
        if sys.stdin.isatty() and sys.stdout.isatty():
            cmd_pick(**flags)
        else:
            cmd_list(**flags)
        return

    cmd = args[0] if args else "list"
    if cmd == "next":  # takes any number of names
        with lock():
            cmd_next(names=args[1:], **flags)
        return
    fn = COMMANDS.get(cmd)
    if not fn:
        # `codexhop work` is a shortcut for `codexhop use work`
        if VALID_NAME.match(cmd) and (os.path.exists(account_path(cmd)) or cmd in list_providers()):
            fn, args = cmd_use, ["use", cmd]
        else:
            accounts, provs = list_accounts(), list_providers()
            known = ", ".join(accounts + provs)
            hint = f" Saved accounts{' and providers' if provs else ''}: {known}." if known else ""
            die(f"unknown command '{cmd}'. Try: {PROG} help{hint}", USAGE_EXIT)
    if len(args) > 3:
        die(f"too many arguments for '{cmd}'", USAGE_EXIT)

    def call():
        fn(name=args[1] if len(args) > 1 else None, new=args[2] if len(args) > 2 else None, **flags)

    if fn in READ_ONLY:
        call()
    else:
        with lock():
            call()


def _run() -> int:
    try:
        main(sys.argv[1:])
    except SystemExit as e:
        if isinstance(e.code, int):
            return e.code
        return 0 if e.code is None else 1
    return 0


def console_main() -> int:
    """Entry point for the `codexhop` command, and for `python -m codexhop`."""
    try:
        code = _run()
        sys.stdout.flush()
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        # `codexhop list | head` - say nothing, leave no traceback.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 0
    return code


if __name__ == "__main__":
    sys.exit(console_main())
