# git-account-setup

Portable setup for separating two GitHub accounts (e.g. personal + work) on
one machine: SSH host aliases, per-repo identity, owner-scoped URL rewriting,
and commit/push guard hooks. Pure Python stdlib (tkinter for the GUI is
optional) — works on Windows, macOS, and Linux.

## Requirements

Python 3.8+, standard library only — no `pip install` needed, no
`requirements.txt`. The GUI wizard additionally needs `tkinter`, which ships
with the standard installer on Windows and macOS; on Linux it's a separate
OS package if missing:

```
sudo apt install python3-tk       # Debian/Ubuntu
sudo dnf install python3-tkinter  # Fedora
```

`--cli` mode needs no GUI dependency at all.

## Run it

```
python setup_git_accounts.py            # GUI wizard
python setup_git_accounts.py --cli      # terminal wizard (no tkinter needed)
python setup_git_accounts.py --cli --dry-run   # preview only, writes nothing
```

You'll be asked, once, for:
- the one folder that should **always** use the personal account (e.g.
  `~/Projects/Personal`) — everything else defaults to work
- personal account: GitHub username, git email, an SSH key (existing path,
  or let it generate a new ed25519 one)
- work account: same, plus you can list extra orgs/owners that also count
  as "work" (e.g. your employer's GitHub org)

## What it sets up

- `~/.ssh/config` — `Host personal` / `Host work` aliases, each pinned to
  its own key with `IdentitiesOnly yes` (so ssh-agent can't offer the wrong
  key to GitHub).
- `~/.gitconfig-personal`, `~/.gitconfig-work` — the actual `[user]`
  name/email for each account.
- `~/.gitconfig` (a clearly marked, replaceable block, your other settings
  are left alone):
  - a default `[user]` identity (work)
  - **owner-scoped** `url.insteadOf` rules: `https://github.com/<owner>/...`
    is rewritten to the right SSH alias based on *who owns the repo*, not
    which folder you happen to be in or how the remote was added. This
    means `git clone`, `git init` + `git remote add`, and GUI clients all
    just work, with no shell wrapper needed.
  - `includeIf` rules so the right `[user]` identity applies based on the
    designated folder, or (once a remote exists) the remote's host alias
    or owner — whichever is known.
- `~/.githooks/{pre-commit,pre-push}` (wired up via `core.hooksPath`) —
  refuse to commit/push if the configured identity doesn't match the
  account the remote belongs to, and pre-push additionally scans the
  commits actually being sent for the wrong author. Every other hook name
  gets a small pass-through stub so repo-local hooks (husky, lint-staged,
  ...) still run.
- git aliases: `git whoami`, `git use-personal`, `git use-work`,
  `git clone-personal`, `git clone-work`.

## Safety

- Every file the script would modify is backed up first, to
  `~/.git-identity-backup-<timestamp>/`.
- Re-running the script is idempotent — it replaces its own previously
  written block/lines instead of duplicating them, and leaves any
  unrelated content in `~/.gitconfig` / `~/.ssh/config` untouched.
- `--cli --dry-run` prints everything it would do without writing anything.

## Why owner-scoped rewriting instead of a folder-only rule

An earlier version of this idea (still in use on one machine) used a single
blanket rule — *any* `https://github.com/...` URL rewritten to the work SSH
alias — plus shell wrapper functions in `.bashrc`/the PowerShell profile to
special-case `git clone` from inside the personal folder. That leaves a gap:
a brand-new personal repo created with a plain `git init` + `git remote add`
(not `git clone`) never goes through the wrapper, so it silently gets
rewritten to the *work* SSH identity and the guard hooks block both the
commit and the push with a confusing "wrong identity" error. Scoping the
`insteadOf` rule per GitHub owner instead of blanket-to-one-account closes
that gap at the source, and makes the shell wrapper unnecessary altogether.
