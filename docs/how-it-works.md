# How it works

codexhop keeps a copy of each account's Codex login and swaps the live one in
and out. This page covers where the login lives, how codexhop tells logins
apart, what a hop does, what happens to Codex sessions that are already open,
and how `run`, `usage` and `add` work, and how a hop to Azure, Bedrock or another provider differs.

The behaviour of Codex described here was read from its source at rust-v0.160.1
and rust-v0.166.0, and at rust-v0.163.0 to rust-v0.165.0 for the two version
cut-offs. The parts marked as measured were checked with real Codex binaries on
2 November 2026.

## Where Codex keeps the login

Codex keeps its login in one file, `$CODEX_HOME/auth.json` (`~/.codex/auth.json`
by default). For a ChatGPT login the file holds an ID token, an access token, a
refresh token, the workspace id Codex sends with each request (`account_id`) and
the time of the last renewal. For an API-key login it holds the key.

Codex can instead keep the login in the system keyring
(`cli_auth_credentials_store` in `config.toml`). codexhop refuses that setting,
because it cannot read the keyring; the file is Codex's default.

An access token lasts 240 hours from when it was issued (measured on a real
token). Codex renews the login on its first request once the access token is
within 5 minutes of running out. It falls back to renewing 8 days after the last
renewal only for an access token whose expiry it cannot read.

Renewing returns a new refresh token. Codex treats the old one as spent: it has
its own handling for a `refresh_token_reused` refusal from the server. That
the server refuses an old refresh token was not measured here.

codexhop saves each account as an exact copy of that file in
`$CODEX_HOME/codexhop/accounts/<name>.json`, mode 600. A hop is a file copy, and
nothing about the login is translated or rebuilt.

## How codexhop knows which account a login is

The ID token and the access token are JWTs, and their claims name the email,
the ChatGPT user and the workspace. codexhop reads those claims without checking
the signature, which needs no network, so `list`, `whoami` and the hop itself
work offline.

Two copies are the same login when they share both the user and the workspace.
One person can belong to several workspaces, and one workspace holds many
people, so neither alone is enough.

The `account_id` field beside the tokens is never trusted on its own. Codex
before 0.164.0 (the check against it first appears in rust-v0.164.0) could write one login's renewed tokens next to another login's
`account_id` (see below), and the tokens are what say whose login it is. When
codexhop files a login whose field disagrees with its tokens, it puts the field
right.

When two copies of one login differ, the one whose access token was issued
later is newer, and an older copy never overwrites a newer one.

## What a hop does

1. Stop if the target is open in a `run` session, or if an open `codex` session
   is about to renew the live login. Both need `--yes` to go past.
2. If the target's access token has run out, renew it and save the result, so
   the login works as it lands.
3. Take Codex's own lock, an exclusive `flock` on `$CODEX_HOME/auth.lock`.
   Codex 0.166.0 holds it while it loads, merges and saves `auth.json`; the lock
   first appears in rust-v0.166.0, and older versions do not take it.
4. Read `auth.json` again under the lock and file it under the account its
   tokens belong to. A login with no saved account yet is saved under its email
   rather than dropped.
5. Write the target login into `auth.json`: a new file, mode 600, `fsync`, then
   `rename`.
6. Release the lock and record the account as active.

codexhop also keeps a lock of its own, `codexhop/.lock`, so two codexhop
commands never interleave their reads and writes. A command left waiting names
the command it waits for.

## What open sessions do

A running Codex reads the login once and keeps it in memory. It reads
`auth.json` again only when it renews, and then only if the file still holds the
same workspace.

[`session-follows-hop.py`](experiments/session-follows-hop.py) measured this. It
runs a real `codex exec` against a local fake backend with the network cut off,
and its full output is in
[`results-2026-11-02.txt`](experiments/results-2026-11-02.txt). Four of its five
cases are about open sessions:

| What happened | Codex 0.160.1 | Codex 0.166.0 |
|---|---|---|
| A session made a request as login A, then codexhop hopped to B | the next request carried A | the next request carried A |
| After the hop, A came due for renewal and the server refused A's token | Codex did not renew A, and the session stopped with "signed in to another account. Please sign in again." | the same |
| The hop landed while Codex was renewing A | A's renewed tokens were saved into `auth.json` under B's workspace id, and the session sent them with B's workspace | A's renewed tokens were dropped and the session went on as B |
| B was another person in A's own workspace | at A's renewal the session took up B's login and went on as B | the same |

So a hop changes the login for new sessions. An open session keeps its login
until that login's access token is within 5 minutes of running out, about 10
days after its last renewal, and then stops. The hop says how long the open
sessions have.

A new session starts on the new account, and `codex resume` reopens the
conversation in it. When both logins are in one workspace, the open session
moves to the new login at that point instead of stopping. To use two accounts at
the same time, use `chop run`.

The third row is why a hop stops when an open session is about to renew the
live login. In both versions Codex had already traded A's refresh token for new
ones, and the new ones did not reach A's saved copy. If the server refuses a
used refresh token, as Codex's handling suggests, that saved copy can no longer
renew and the account has to sign in again.

On 0.160.1 codexhop recovers the tokens: the next hop or `doctor --fix` files
them under A by their claims and puts B back. On 0.166.0 they are gone, which is
why the hop stops beforehand rather than repairing afterwards.

## Renewing

A saved login nobody uses is renewed by nobody, and a refresh token can expire
(Codex has its own handling for `refresh_token_expired`). `chop renew` renews
the logins whose access token runs out within a day, a day ahead of when Codex
would, and writes each result to the account's file before anything else.

The live login is renewed too, under `auth.lock`, unless a `codex` session has
it open. In that case the session renews it.

## Run sessions

`chop run <name>` gives one terminal its own account. The session runs with
`CODEX_HOME` set to `codexhop/run/<name>/`, which holds the account's
`auth.json` and its own `auth.lock`.

Every other entry of your real Codex home, except the per-login files listed
below, is a symlink in the run home. So the session has your config, rules,
skills, history, sessions and SQLite state, and `codex resume` sees the same
threads in both.

Codex follows a symlinked `config.toml` to the real file when it saves settings
(from its source), so the link stays a link. SQLite puts its `-wal` and `-shm`
files beside the real database when it opens one through a symlink. With Codex
0.160.1 and 0.166.0, a run session left only `auth.json`, `auth.lock` and
codexhop's own markers as real files in its home (measured, the `run-home` case).

The run home leaves out the login and what Codex keeps per login: the model list
(`models_cache.json`, which differs by plan), the cloud config cache (bound to a
user and workspace) and the app-server daemon's folders, which would carry the
main login.

When the session ends, a login it renewed is saved back to the account. A marker
file in the run home names the real Codex home, so `codexhop` typed inside a run
session still acts on your real setup.

## Usage

`chop usage` asks `https://chatgpt.com/backend-api/wham/usage`, the endpoint
Codex's app server reads an account's rate limits from (from its source), with
the account's access token and workspace header.
The reply names two windows, which codexhop shows by their length: the 5-hour
one and the weekly one.

A reading is kept in `codexhop/usage.json` with percentages and times only. A
window whose reset time has passed shows as empty without asking the server
again.

## Adding an account

`chop add <name>` runs `codex login` with `CODEX_HOME` pointing at a temporary
folder inside the `codexhop` folder, with `cli_auth_credentials_store = "file"`
in its config. It copies the resulting `auth.json` out and deletes the folder.

The live login is never touched during the sign-in; the
[reference](commands.md#chop-add-name-codex-login-options) says when the new
login becomes live.

## Providers

A provider is somewhere other than a ChatGPT login for Codex to get its model
from. Codex describes one in a profile file, `$CODEX_HOME/<name>.config.toml`,
and `codex --profile <name>` layers that file over `config.toml`. codexhop calls
a profile file a provider when it sets `model_provider` to something other than
`openai`, so Azure OpenAI, Amazon Bedrock and any endpoint you add by hand all
count. `chop provider add` only writes such a file; nothing else about it is
codexhop's.

A hop to an account swaps `auth.json`. A hop to a provider cannot work that way,
because there is no login to swap, and it cannot edit `config.toml` either.
Measured with Codex 0.166.0 on 6 November 2026: a `profile = "<name>"` line in
`config.toml` stops Codex at start with "legacy `profile` config is no longer
supported; use `--profile`", so no default profile can be set there. Writing
`model_provider` into `config.toml` would collide with whatever the file already
sets, and codexhop does not edit a file you maintain.

So the hop is a pointer in `codexhop/provider`, and the reading side is the
`codex` function that `shell-init --wrap-codex` defines. It calls `chop launch`,
which runs `codex --profile <name>` when the pointer names a provider and plain
`codex` when it does not. A command that already carries `--profile` or `-p`
runs as typed. So do the commands `--profile` does not apply to. Codex 0.166.0
names where it does apply in its own error message (the interactive screen,
`exec`, `review`, `resume`, `queue`, `archive`, `delete`, `unarchive`, `fork`,
`mcp`, `sandbox`, `migrate-nfs-db` and `debug prompt-input`) and, measured with
`doctor`, refuses it everywhere else.

The ChatGPT login is not touched by a hop to a provider or back. It is not read,
renewed or moved, so a hop back needs none of the checks an account hop makes.
Sessions that are already open should not be affected either way, since the
profile is read when Codex starts. That was inferred from how `--profile` works
and was not measured.

Codex started any way other than through that function, such as an editor
extension or an app, does not read the pointer (inferred from how shell functions
work; not tested with any editor). `chop run <provider>` is the explicit
form: it replaces itself with `codex --profile <name>` and changes nothing.

Keys stay out of codexhop. A profile names the environment variable Codex reads
an Azure key from (`env_key`), and Bedrock takes its credentials from the AWS
settings Codex finds on its own (`aws.profile` and `aws.region` in the profile,
or `AWS_PROFILE`, `AWS_REGION` and the files under `~/.aws`). A launcher, set
with `chop provider set`, is a command codexhop runs in place of `codex
--profile`, for the case where something else has to put the key in the
environment first.

What codexhop prints about a provider is filtered by shape, not by knowing the
key. A variable name is accepted only if it is one case, has at most four digits,
is at most 40 characters and, past 24, contains an underscore. An address is shown without its user name,
password, query and fragment, and not at all if that cannot be done safely. A
launcher's `NAME=value` word is masked when the name contains `KEY`, `TOKEN`,
`SECRET`, `PASSW`, `CREDENTIAL` or `AUTH`. These catch the usual mistakes. A key
that matches none of these shapes, written by hand into a profile or passed to a
launcher as an ordinary argument, would still be listed.

Checked on 6 November 2026 with Codex 0.166.0 started through the real function
after a hop: the header showed the Azure profile's model, and for a Bedrock
profile with no model set, Codex's own Bedrock default. That checks the
profile is picked up. It does not check a request: none was sent to Azure or
Bedrock.

## What codexhop does not do

It never hops by itself, keeps no background process, and sends nothing to any
address except the token and usage endpoints above. It does not read or write
the keyring. Windows is not supported.
