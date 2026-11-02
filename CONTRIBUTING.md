# Contributing

Thanks for looking. codexhop is small on purpose, and a few rules keep it that
way. A pull request that follows them is usually merged quickly.

## The rules that keep it small

1. **Python 3.9+, standard library only.** No dependencies and no build step.
   `codexhop.py` stays a single file you can read in one sitting and run
   straight from a clone. `pyproject.toml` packages that one module and nothing
   else.
2. **Never lose a login.** Any code path that replaces the live login first
   files what was there under the account it belongs to, by its token claims;
   one with no saved account yet is stashed under its email. Codex treats a used
   refresh token as spent, so a renewed login is written to disk before anything
   else happens, and a login that a running Codex holds is renewed only by that
   Codex.
3. **Tests use fake logins and no network.** `test/test_codexhop.py` runs the
   real command against a throwaway `CODEX_HOME`, with unsigned made-up tokens,
   a local fake token and usage server, and a stand-in `codex` script. A test
   that needs a real account is not a test we can run.
4. **Say what you measured.** A claim about how Codex behaves names the Codex
   version it was checked on and how. The ones in the docs come from
   `docs/experiments/session-follows-hop.py`, which runs a real Codex binary
   against a local fake backend with the network cut off (`unshare -rn` on
   Linux).
5. **Tokens never get printed.** Not in `--json`, not in errors, not in a cache
   file, not in a test's failure message.

## Working on it

```bash
python3 -m unittest discover -s test          # the whole suite, about 15 seconds
TEST_PYTHON=python3.9 python3.9 -m unittest discover -s test   # the oldest supported Python
ruff check codexhop.py test/ docs/experiments/
shellcheck -S warning install.sh
```

`./install.sh` links `~/.local/bin/codexhop` to your clone, so your edits take
effect at once. CI runs the suite on Linux and macOS with Python 3.9 and 3.13,
builds and installs the package, and checks the installer.

## Things worth doing

- macOS on real hardware. CI runs the tests on macOS, where finding running
  `codex` processes falls back to `pgrep`, which cannot see which Codex home
  each one uses. Driving a run session on a Mac and reporting what happens is
  the next step.
- The keyring store. Codex can keep its login in the system keyring instead of
  `auth.json`; codexhop refuses that today. Supporting it means reading and
  writing the `Codex Auth` item per Codex home.
- fish shell completion. The shell glue covers bash and zsh.

## Style

Match the code around you: plain names, comments that say why, and messages a
person can act on without reading the source.
