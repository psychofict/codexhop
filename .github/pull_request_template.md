## What this changes

<!-- One or two sentences: what a user will see differently, and why. -->

## How it was checked

- [ ] `python3 -m unittest discover -s test` passes
- [ ] `ruff check codexhop.py test/ docs/experiments/` is clean
- [ ] `shellcheck -S warning install.sh` is clean
- [ ] A new or changed behaviour has a test in `test/test_codexhop.py`
- [ ] `README.md`, `docs/commands.md` and `CHANGELOG.md` are updated if a user can see the change

If this says something new about how Codex behaves, name the Codex version you
measured it on and how (see rule 4 in CONTRIBUTING.md).

Never paste a token, an `auth.json` or the contents of `~/.codex/codexhop/` into a pull request.
