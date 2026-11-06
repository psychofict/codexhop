# Command reference

`codexhop` and `chop` are the same command; `chop` is the alias the shell glue
adds (`eval "$(codexhop shell-init)"`). Every command works on the Codex home in
`$CODEX_HOME`, `~/.codex` by default.

## Choosing an account

### `chop`

In a terminal, lists your accounts and providers, with each account's last
5-hour reading, and asks which one to hop to. Type a number, a name or the start
of one; Enter stays where you are.

With fewer than two accounts and providers, or when the output is not a
terminal, it prints the listing instead and asks nothing. The menu takes codexhop's lock only after
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

If the name is a provider and not an account, the hop sets the provider pointer
instead (see [Providers](#providers)). Hopping to an account clears it and says
`left provider <name>`. When a name is both, the account wins, and `chop doctor`
says so.

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
codex's. For a provider, `chop run <provider>` replaces itself with
`codex --profile <name>` (see [Providers](#providers)).

## Providers

A provider is a Codex profile file, `$CODEX_HOME/<name>.config.toml`, that sets
`model_provider` to something other than `openai`. Files that do not set it are
ignored, and so are profiles whose name is not a valid account name. A profile
you wrote yourself is listed like one codexhop wrote. Codex may add a `[tui]`
table to the file (it did in testing with 0.166.0); codexhop reads the file and
never rewrites it.

A hop to a provider is a pointer, `codexhop/provider`, and leaves `auth.json`
alone. Codex 0.166 rejects a default `profile = "<name>"` in `config.toml`, so
the pointer is read by `chop launch`, which the `codex` function from
`chop shell-init --wrap-codex` calls. See
[how it works](how-it-works.md#providers).

### `chop provider add <name> (--azure ENDPOINT | --bedrock | --base-url URL) [options]`

Writes `$CODEX_HOME/<name>.config.toml` (mode 600). It refuses to overwrite a
file that exists and to use the name of an account or a command. With
`--base-url` the name is also the provider's id in the file, so it cannot be one
Codex has built in (`openai`, `amazon-bedrock`, `ollama`, `lmstudio`, `gcp`).

| Option | Meaning |
|---|---|
| `--azure ENDPOINT` | Azure OpenAI. `https://<resource>.openai.azure.com` gets `/openai/v1` added; a full address is used as given. Needs `--model`, because Azure runs the deployment you name. |
| `--bedrock` | Amazon Bedrock. Credentials and region come from your AWS settings unless you pass the next two. |
| `--region REGION`, `--aws-profile PROFILE` | `--bedrock` only: written to the profile's `aws` table. |
| `--base-url URL` | Any other endpoint that speaks the Responses API. |
| `--model MODEL` | the model, or for Azure the deployment name. Left out for Bedrock and `--base-url`, Codex picks its own default. |
| `--env-key VAR` | the variable Codex reads the key from. Azure defaults to `AZURE_OPENAI_API_KEY`; with `--base-url` there is none unless you name one. Not for `--bedrock`. It must be a variable name: letters, digits and `_`, in one case, with at most four digits and at most 40 characters, and an underscore if it is longer than 24. Anything else is refused without being repeated, because it may be a key. A real name that is refused can still be written into the profile by hand; codexhop then neither lists it nor checks that it is set. |
| `--launcher COMMAND` | see `provider set`. |

codexhop writes no key into the file and keeps none. An option it does not know
is reported by name only, so `--api-key sk-...` typed by mistake is not echoed.

### `chop provider [list]`

Name, provider id, host or region, model and key status of every provider. The
KEY column says whether the variable the profile reads is set in this shell
(never its value), or shows the launcher. `*` marks the one hopped to.
`--json` returns `{"codexHome", "provider", "providers": [...]}`.

### `chop provider set <name> --launcher COMMAND`

A launcher is a command that starts Codex on that provider and receives Codex's
arguments, for when a vault or helper supplies the key. With one set, `chop run`
and the `codex` function run `COMMAND <arguments>` instead of
`codex --profile <name> <arguments>`. `--launcher ''` removes it. It is kept in
`codexhop/providers.json`.

Keep the key out of the command. A launcher that sets a variable whose name
contains `KEY`, `TOKEN`, `SECRET`, `PASSW`, `CREDENTIAL` or `AUTH` to a value,
as in `env AZURE_OPENAI_API_KEY=... codex`, is refused, because the launcher is
saved and listed. Names that end in `_FILE` or `_PATH` are fine, and so is a
value that is a path, such as `GOOGLE_APPLICATION_CREDENTIALS=/home/me/key.json`
(it starts with `/`, `~/` or `./`), and so is
`env CODEX_AZURE_PROFILE=azure-agents codex-azure`. The check reads variable
names and the start of the value only; a key passed as an ordinary argument is
not recognised.

### `chop provider rm <name>`, `chop rm <name>`

Deletes the profile file if its first line says codexhop wrote it, after asking
at a terminal (`--yes` skips the question), and clears the pointer if it named
this provider.
A file you wrote yourself is left for you to delete.

### `chop launch [--] [codex arguments]`

Starts `codex` the way the hop says. With a provider hopped to, it runs
`codex --profile <name> <arguments>`, or the provider's launcher; otherwise plain
`codex`. It leaves alone any command that already names a `--profile` or `-p`,
and the commands `--profile` does not apply to (`login`, `logout`, `update`,
`doctor` and the rest). It warns on standard error when the profile's key
variable is not set. The exit status is codex's.

You do not type this; the `codex` function does.

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

Name, email and plan of every saved account, then every provider with its
provider id, host or region and model, and `provider` in the PLAN column. `*`
marks what new `codex` sessions use: the provider hopped to, or else the live
login. When one email appears in two workspaces, a WORKSPACE column tells them
apart.

`--long` adds when each access token runs out, when it was last renewed and when
the account was saved. No network.

### `chop whoami`

The live login: email, plan, the account it is saved as, and how long its access
token has left. It also warns when `CODEX_ACCESS_TOKEN` is set, or
`CODEX_API_KEY`, which `codex exec` reads, because those take the place of the
saved login. With a provider hopped to, it adds a line naming the provider, and
with no ChatGPT login at all it names the provider and exits 0. No network.

### `chop active`

Prints the provider hopped to, or else the live account's name, or exits 1 if
there is neither. Made for prompts and status lines. `--json` gives both:
`{"active": <live account>, "provider": <provider or null>, "codexHome"}`. No
network.

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
- `CODEX_API_KEY` or `CODEX_ACCESS_TOKEN` set in the environment;
- a hop to a provider whose profile file is gone (`--fix` clears the pointer),
  a name that is both an account and a provider, and, for the provider hopped
  to, a key variable that is not set in this shell, or a `env_key` that is not a
  variable name (it may be a key), and a shell that has not loaded the
  `--wrap-codex` glue, which plain `codex` needs to follow the hop. The glue check
  goes by the `CODEXHOP_WRAP` variable, which a child shell inherits without the
  function, so it can say the glue is loaded when it is not.

Exits 1 if an error is left unfixed. It prints no tokens.

### `chop shell-init [--alias NAME] [--wrap-codex]`

Prints the shell glue for bash and zsh: the `chop` alias (or `NAME`) and tab
completion for commands, flags, account names and profile names. Put
`eval "$(codexhop shell-init)"` in your shell rc.

With `--wrap-codex` it also exports `CODEXHOP_WRAP=1` and defines a `codex`
function that calls `chop launch`, so plain `codex` follows a hop to a provider.
If `codexhop` is not on the PATH the function falls back to plain `codex`.

## Flags

| Flag | Meaning |
|---|---|
| `-y`, `--yes` | never prompt; also goes past the open-session stops |
| `--long`, `-l` | more columns in `list` |
| `--json` | machine-readable output for `list`, `whoami`, `active`, `usage` and `doctor`; never a token |
| `--no-cache`, `--fresh` | `usage`: ask the server even if the last reading is under a minute old |
| `--fix` | `doctor`: repair what it can |
| `--alias NAME` | `shell-init`: the short command's name |
| `--wrap-codex` | `shell-init`: also define a `codex` function that follows a hop to a provider |
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
| `provider` | the name of the provider hopped to; absent when there is none |
| `providers.json` | each provider's launcher, if it has one. `provider set` refuses one that sets a key-looking variable; if you edit one in by hand, it is listed as `NAME=***` |
| `next` | the logins `chop next` may choose from, one per line |
| `usage.json` | the last usage reading per account: percentages and times, no tokens |
| `run/<name>/` | the Codex home a `chop run` session uses |
| `.lock` | codexhop's own lock; it names the command holding it |

## JSON

`chop list --json` returns `{"codexHome", "active", "provider", "accounts": [...],
"providers": [...]}`. Each provider has `name`, `active`, `provider`, `model`,
`baseUrl` (no user name, password, `?query` or `#fragment`, with the path as
written; `null` when the address has no host or a non-ASCII one, has a port that
is not a number, or does not parse), `region`, `awsProfile`, `envKey` (the variable's name;
`null` when the profile's value looks like a key), `launcher` (a key-looking
`NAME=value` shows as `NAME=***`; `null` when it is not a command) and `file`. Each account has `name`, `active`, `kind` (`chatgpt` or
`apikey`), `email`, `plan`, `userId`, `workspaceId`, `expiresAt` and
`lastRefresh` (epoch seconds), `renewalDue` and `savedAt`.

`chop usage --json` returns `{"accounts": [...]}`; each has `name`, `active`,
`fetchedAt`, `throttledUntil`, `note` and `usage`, which holds `plan`, `short`
and `long` (each `{"pct", "seconds", "resetsAt"}`), `limitReached`, `extra` (other
meters, each `{"name", "short", "long"}`) and `credits`.

## Exit status

`0` success; `1` an error, a refusal or a failed renewal; `2` a usage mistake
(an unknown command or option, a missing argument). `chop run` exits with
codex's status.
