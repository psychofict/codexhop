# Security

## What this tool holds

`~/.codex/codexhop/accounts/<name>.json` is a full, live Codex login: the same
`auth.json` Codex keeps, with an ID token, an access token and a refresh token
(or an API key, for an API-key login). Anyone who can read those files can use
Codex as you until the login is signed out. Treat them like an SSH private key.

What the tool does about that:

- The `codexhop` folder and `accounts/` are created `700`, every saved login is
  written `600`, and the folders are tightened again on every run if something
  loosened them. `auth.json` is written `600` as well.
- Writes are atomic: a new file with the right mode, `fsync`, then `rename`, so
  a crash mid-write cannot leave a half-written or world-readable login.
- While it reads the live login and writes the next one, codexhop holds Codex's
  own lock on `auth.lock`, the one Codex 0.166.0 takes around its writes, so a
  renewal Codex has already saved is never overwritten.
- A renewed login is written to the account's file before anything else
  happens. The reply carries a new refresh token, and Codex treats the old one as
  spent, so a copy left holding it may have to sign in again.
- `--json`, `doctor`, the usage cache (`usage.json`) and every error message
  carry email addresses, plans, workspace ids and times, never a token.
- Which account a login is gets read from its own ID token. The token's
  signature is not checked, because codexhop only tells your own saved logins
  apart; it never decides whether to trust a login.
- The tool talks to two addresses, and only when a command needs them:
  `POST https://auth.openai.com/oauth/token` to renew a login (from `renew`,
  from `usage` for an idle account whose access token has run out, or from a hop
  to such an account), and `GET https://chatgpt.com/backend-api/wham/usage` for
  `usage` and `next`.
- Requests carry a codexhop User-Agent. A renewal sends Codex's public OAuth
  client id, as renewing any Codex login must. `CODEXHOP_OFFLINE=1` turns off
  both kinds of request.
- `codexhop add` runs `codex login` in a private temporary Codex home inside the
  `codexhop` folder and deletes it afterwards, so a sign-in never touches the
  live login.
- A `codexhop run` home holds its account's login and links to your real Codex
  home for everything else. Its marker file names that real home and nothing
  secret.

## How releases are made

Releases are built by GitHub Actions and uploaded to PyPI through trusted
publishing. PyPI checks that each upload comes from this repository's release
workflow and its `pypi` environment, so no PyPI token is stored anywhere to
steal.

Each publish also waits for the maintainer's approval in that environment.

## Known limits

- **The keyring.** If Codex is set to keep its login in the system keyring
  (`cli_auth_credentials_store = "keyring"` or `"auto"`), codexhop refuses to
  hop and says how to switch to the file, which is Codex's default.
- **Codex before 0.164.0.** These versions save a renewal without checking that
  `auth.json` still holds the same login. codexhop stops a hop while an open
  session is about to renew, and `doctor --fix` repairs a file that was mixed
  anyway; upgrading Codex closes the gap.
- **Root and same-user processes.** Anything running as your user can read the
  files, and can read the environment of a running `codex`. This tool does not
  and cannot defend against that.
- **`git`.** `auth.json`, `accounts/` and `*.bak` are in `.gitignore`. Do not
  move a saved login into a repository.

## Reporting a vulnerability

Open a [security advisory](https://github.com/psychofict/codexhop/security/advisories/new)
on the repository. Please don't file a public issue for anything that could
expose a login. Expect a first reply within a week.
