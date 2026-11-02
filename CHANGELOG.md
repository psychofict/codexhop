# Changelog

All notable changes to codexhop are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

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
