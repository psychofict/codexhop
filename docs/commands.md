# Command reference

`codexhop` and `chop` are the same command; `chop` is the alias the shell glue
adds (`eval "$(codexhop shell-init)"`). Every command works on the Codex home in
`$CODEX_HOME`, `~/.codex` by default.

## Choosing an account

### `chop`

In a terminal, lists your accounts with their last 5-hour reading and asks which
one to hop to. Type a number, a name or the start of one; Enter stays where you
are.

With fewer than two accounts, or when the output is not a terminal, it prints
the listing instead and asks nothing. The menu takes codexhop's lock only after
you answer, so a menu left open never holds up another command.

### `chop <name>`, `chop use <name>`

Hops to that account: files the live login under the account it belongs to,
then writes the saved login into `auth.json`. New `codex` sessions start as the
new account.

Sessions that are already open keep the login they started with, and the hop
says what happens to them and when (see
[how it works](how-it-works.md#what-open-sessions-do)).

It stops with nothing changed when:

- the account is open in a `chop run` session, because two copies of one login
  renew apart and one of them is signed out (`--yes` goes ahead anyway);
- a `codex` session has the live login open and that login's access token is
  within 5 minutes of running out, so the session's next request renews it and a
  hop at that moment can cost the account its login (`--yes` goes ahead anyway);
- Codex holds `auth.lock` for longer than `CODEXHOP_LOCK_WAIT` seconds;
- Codex keeps its login in the keyring rather than in `auth.json`.

If the account's access token has run out, the hop renews it first and saves the
result, so the login works as it lands.

### `chop next [names...]`

Hops to another login in the list, the one with the most room in its 5-hour
window. The first time, name the logins it may choose from; they are kept in
`codexhop/next` and used from then on. The live login is never chosen.

It reads usage for each of the others first (see `usage`) and skips any whose
5-hour window is 95% used or whose weekly limit is reached. It ranks the rest by
5-hour use in bands of ten points, and within a band it picks the one whose
window resets soonest, since what that window has left is lost at the reset. It
never runs by itself.

### `chop run <name> [codex arguments]`

Runs `codex` as that account in this terminal only, without touching the live
login. Everything after the name goes to `codex`, so `chop run work resume
--last` works.

The session gets its own Codex home, `codexhop/run/<name>/`, holding the
account's login and links to everything else in your Codex home, so it has your
config, rules, skills, history and sessions. When the session ends, a login it
renewed is saved back to the account.

If the account is the live one, this is plain `codex`. The exit status is
codex's.

## Adding and keeping accounts

### `chop add <name> [codex login options]`

Signs in with `codex login` in a private temporary Codex home and saves the
result as `<name>`. Your live login is not touched during the sign-in. The new
login becomes live when there was none, or when it is a fresh sign-in for the
account that is already live.

Options after the name go to `codex login`: `--device-auth` signs in with a code
instead of a browser redirect, and `--with-api-key` reads an API key from
standard input.

If your browser is already signed in to ChatGPT as another account, that is the
account you get, so open the sign-in link in a private window. Signing in as an
account that is already saved under another name saves nothing and says which
name has it.

### `chop save [name]`

Saves the login you are using now. Without a name it uses the email's local
part. A login that is already saved under another name is refused, because two
copies renew apart; `rename` gives it the new name instead.

### `chop renew [name]`

Renews the saved logins whose access token runs out within a day, a day ahead
of when Codex would renew them. With a name it renews that one, due or not.

The live login is left alone while a `codex` session has it open, and an account
open in a `run` session is skipped; `--yes` overrides both. Exits 1 if any
renewal failed.

### `chop rm <name>`

Deletes the saved copy and its run home. It does not sign you out of the live
login.

### `chop rename <old> <new>`

Renames an account, its run home, its place in the `next` list and its cached
usage.

### `chop sync`

Writes the live login back to its saved copy. Hops and `doctor --fix` do this
anyway; it is here for scripts.

## Looking

### `chop list`

Name, email and plan of every saved account; `*` marks the live one. When one
email appears in two workspaces, a WORKSPACE column tells them apart.

`--long` adds when each access token runs out, when it was last renewed and when
the account was saved. No network.

### `chop whoami`

The live login: email, plan, the account it is saved as, and how long its access
token has left. It also warns when `CODEX_ACCESS_TOKEN` is set, or
`CODEX_API_KEY`, which `codex exec` reads, because those take the place of the
saved login. No network.

### `chop active`

Prints the live account's name, or exits 1 if the live login is not saved. Made
for prompts and status lines. No network.

### `chop usage [name]`

How much of each account's 5-hour and weekly limit is used, and when each
window resets, from the endpoint Codex reads an account's rate limits from.

A reading less than a minute old is shown again without asking (`--fresh` asks
anyway), and a server that answers 429 is not asked again before its
`Retry-After`. An idle account whose access token has run out is renewed first;
the live login is not, while a `codex` session has it open.

Other meters the server reports, such as a separate limit for one model, show in
the NOTE column; credits are in `--json`.

### `chop doctor [--fix]`

Checks the setup and says what is wrong:

- the Codex version, with a warning before 0.164.0;
- where Codex keeps its login, which must be the file;
- folder and file permissions (`--fix` sets 700 and 600);
- an `auth.json`, or a saved login, whose `account_id` field names another
  workspace than its tokens (`--fix` files the tokens under their own account and
  puts the pointer's account back live);
- two saved accounts holding one login;
- logins due for renewal, and saved copies older than the live login or a run
  home's (`--fix` saves the newer one);
- a pointer to an account that is gone, run homes of deleted accounts and
  leftover temporary files (`--fix` clears them);
- `CODEX_API_KEY` or `CODEX_ACCESS_TOKEN` set in the environment.

Exits 1 if an error is left unfixed. It prints no tokens.

### `chop shell-init [--alias NAME]`

Prints the shell glue for bash and zsh: the `chop` alias (or `NAME`) and tab
completion for commands, flags and account names. Put
`eval "$(codexhop shell-init)"` in your shell rc.

## Flags

| Flag | Meaning |
|---|---|
| `-y`, `--yes` | never prompt; also goes past the open-session stops |
| `--long`, `-l` | more columns in `list` |
| `--json` | machine-readable output for `list`, `whoami`, `active`, `usage` and `doctor`; never a token |
| `--no-cache`, `--fresh` | `usage`: ask the server even if the last reading is under a minute old |
| `--fix` | `doctor`: repair what it can |
| `--alias NAME` | `shell-init`: the short command's name |
| `--no-color` | plain text (so does `NO_COLOR`) |
| `-V`, `--version` | print the version |

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CODEX_HOME` | `~/.codex` | the Codex home, as Codex reads it |
| `CODEXHOP_DIR` | `$CODEX_HOME/codexhop` | where codexhop keeps saved logins and its state |
| `CODEXHOP_OFFLINE` | unset | `1` means no network call at all |
| `CODEXHOP_TIMEOUT` | `10` | seconds before a network call gives up |
| `CODEXHOP_LOCK_WAIT` | `10` | seconds to wait for Codex's `auth.lock` |
| `CODEXHOP_USAGE_URL` | from `chatgpt_base_url` | the usage endpoint, for testing |
| `CODEX_REFRESH_TOKEN_URL_OVERRIDE` | Codex's | the token endpoint; Codex reads the same variable |

## Files

Everything lives in `$CODEX_HOME/codexhop/`:

| Path | Holds |
|---|---|
| `accounts/<name>.json` | the account's login, exactly as Codex writes `auth.json` (600) |
| `active` | the name of the account codexhop last made live |
| `next` | the logins `chop next` may choose from, one per line |
| `usage.json` | the last usage reading per account: percentages and times, no tokens |
| `run/<name>/` | the Codex home a `chop run` session uses |
| `.lock` | codexhop's own lock; it names the command holding it |

## JSON

`chop list --json` returns `{"codexHome", "active", "accounts": [...]}`; each
account has `name`, `active`, `kind` (`chatgpt` or `apikey`), `email`, `plan`,
`userId`, `workspaceId`, `expiresAt` and `lastRefresh` (epoch seconds),
`renewalDue` and `savedAt`.

`chop usage --json` returns `{"accounts": [...]}`; each has `name`, `active`,
`fetchedAt`, `throttledUntil`, `note` and `usage`, which holds `plan`, `short`
and `long` (each `{"pct", "seconds", "resetsAt"}`), `limitReached`, `extra` (other
meters, each `{"name", "short", "long"}`) and `credits`.

## Exit status

`0` success; `1` an error, a refusal or a failed renewal; `2` a usage mistake
(an unknown command or option, a missing argument). `chop run` exits with
codex's status.
