# Changelog

All notable changes to codexhop are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] - 2026-11-09

### Added

- Hop to Azure OpenAI, Amazon Bedrock or another endpoint that speaks the
  Responses API, from the same `chop` menu and `chop <name>` command. A provider
  is a Codex profile file (`~/.codex/<name>.config.toml`), so existing profiles
  are listed with no setup. Checked with Codex 0.166.0: it starts on the
  profile's model. No request to Azure or Bedrock has been tested.
- `chop provider add <name> --azure ENDPOINT --model DEPLOYMENT`, `--bedrock
  [--region R] [--aws-profile P]` and `--base-url URL [--env-key VAR]` write that
  file. `chop provider [list]`, `set` and `rm` list, change and delete providers.
  No key is stored by codexhop. `--env-key` refuses a value that is not a
  variable name or looks like a key, an option it does not know is reported by
  name without its value (a mistyped command or name, or an option typed in the
  place of one, is still repeated back),
  and a listing leaves out a key-like `env_key`, an
  address's user name, password and query, and masks a key-like `NAME=value` in a
  launcher.
- `chop run <provider>` runs Codex on a provider in one terminal.
- `chop shell-init --wrap-codex` defines a `codex` function that follows a hop to
  a provider, which Codex 0.166's missing default-profile setting makes
  necessary. It calls the new `chop launch`.
- A `providers.json` that cannot be used (not JSON, not UTF-8, not an object, or
  a directory) is never rewritten: `provider add`, `set` and `rm` stop first.
  `chop run`, `chop launch` and `chop doctor` say that its launchers are not used.
- An address whose port is not plain digits from 0 to 65535, or whose host has a
  symbol or punctuation mark outside ASCII, is refused by `provider add` and not
  listed. An internationalised host name made of letters still shows.
- `chop provider set <name> --launcher COMMAND` starts Codex through a command,
  for keys held in a vault. It refuses a launcher that sets a variable such as
  `..._KEY` or `..._TOKEN` to a value.
- `chop doctor` checks the provider pointer, name clashes, the key variable and
  the shell glue.
- `chop list --json`, `chop active --json`, `chop whoami --json` and
  `chop doctor --json` report the provider.

### Changed

- `chop active` prints the provider hopped to, if there is one, before the live
  account.
- A name cannot be both an account and a provider; `chop add`, `save` and
  `rename` refuse one that is already a provider.

## [1.0.0] - 2026-11-02

The first release.

### Added

- Hop the live Codex login between saved ChatGPT accounts with `chop <name>`,
  or pick one from a menu with `chop`. API-key logins hop the same way.
- `chop add <name>` signs in with `codex login` in a private temporary Codex
  home, so the live login is untouched while you sign in. Options after the name
  go to `codex login`, such as `--device-auth`.
- `chop run <name>` runs `codex` as one account in one terminal, from a Codex
  home that links to your real one for config, rules, skills, history and
  sessions.
- `chop usage` reads the 5-hour and weekly usage of every saved account, and
  `chop next` hops to another login in a list you give it that has room in its
  5-hour window, only when you run it.
- `chop renew` renews the saved logins whose access token runs out within a
  day, a day ahead of when Codex would renew them.
- `chop doctor [--fix]` checks the Codex version, where Codex keeps its login,
  file permissions, duplicate logins, logins due for renewal and leftover files,
  and repairs an `auth.json` whose workspace id does not match its tokens.
- A login is identified by the email, user and workspace in its own ID token, so
  listing, hopping and `whoami` need no network, and the bare `account_id` field
  is never trusted on its own.
- A hop holds Codex's `auth.lock`, files the outgoing login under its account
  first, and stops while an open session is about to renew the live login.
- `docs/experiments/session-follows-hop.py` measures what open sessions do after
  a hop against real Codex binaries; the results for 0.160.1 and 0.166.0 are in
  `docs/how-it-works.md`.
