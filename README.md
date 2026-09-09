# git-account-setup

Portable setup for keeping multiple git hosting accounts separate on one
machine — any mix of GitHub, GitLab, or self-hosted servers: SSH host
aliases, per-repo identity, owner-scoped URL rewriting, and commit/push
guard hooks. Pure Python stdlib (tkinter for the GUI is optional) — works
on Windows, macOS, and Linux.

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

Add as many accounts as you want. For each one you give:
- a short **label** (used for the SSH alias and git aliases, e.g.
  `personal`, `work`, `client1`)
- a **git host** — `github.com`, `gitlab.com`, or any self-hosted domain
- one or more **owners/orgs** on that host that belong to this account
- git author name + email
- an **optional dedicated folder** — repos under it always use this
  account, even before a remote exists
- an SSH key (existing path, or let it generate a new ed25519 one)

At most one account can be marked **default** — used for repos that live
outside every dedicated folder and don't (yet) have a matching remote.
Everything else is identified by matching the remote's host + owner against
whichever account claims them, regardless of which folder the repo is in.

## What it sets up

- `~/.ssh/config` — one `Host <label>` alias per account, each pinned to
  its own key with `IdentitiesOnly yes` (so ssh-agent can't offer the wrong
  key to the wrong host).
- `~/.gitconfig-<label>` per account — the actual `[user]` name/email.
- `~/.gitconfig` (a clearly marked, replaceable block, your other settings
  are left alone):
  - a default `[user]` identity, if you designated one account as default
  - **owner-scoped** `url.insteadOf` rules per (host, owner): any
    `https://<host>/<owner>/...` remote is rewritten to the right SSH alias
    based on *who owns the repo*, not which folder you happen to be in or
    how the remote was added. This means `git clone`, `git init` +
    `git remote add`, and GUI clients all just work, with no shell wrapper
    needed, for GitHub, GitLab, or a self-hosted server alike.
  - `includeIf` rules so the right `[user]` identity applies based on each
    account's designated folder, or (once a remote exists) its host alias
    or owner — whichever is known.
- `~/.githooks/{pre-commit,pre-push}` (wired up via `core.hooksPath`) —
  refuse to commit/push if the configured identity doesn't match the
  account the remote belongs to (matching is by host + owner, so this works
  identically for GitHub, GitLab, and self-hosted remotes), and pre-push
  additionally scans the commits actually being sent for an author from any
  *other* configured account. Every other hook name gets a small
  pass-through stub so repo-local hooks (husky, lint-staged, ...) still run.
- git aliases per account: `git whoami`, `git use-<label>`,
  `git clone-<label>`.

## Remembering your last answers

On a successful apply, your answers — labels, hosts, names, emails, owners,
folders, key **paths** (never key contents) — are cached to
`~/.git-account-setup-cache.json`. Next time you open the wizard, every
account and field is prefilled from that cache, so you can see your last
configuration and just tweak or re-confirm it instead of retyping everything
(`--dry-run` doesn't update the cache).

## Safety

- Every file the script would modify is backed up first, to
  `~/.git-identity-backup-<timestamp>/`.
- Re-running the script is idempotent — it replaces its own previously
  written block/lines instead of duplicating them, and leaves any
  unrelated content in `~/.gitconfig` / `~/.ssh/config` untouched.
- Labels must be unique, and no two accounts may claim the same
  (host, owner) pair — the wizard validates this before writing anything.
- `--cli --dry-run` prints everything it would do without writing anything.

## Why owner-scoped rewriting instead of a folder-only rule

An earlier version of this idea (still in use on one machine, GitHub-only,
fixed to exactly two accounts) used a single blanket rule — *any*
`https://github.com/...` URL rewritten to the work SSH alias — plus shell
wrapper functions in `.bashrc`/the PowerShell profile to special-case
`git clone` from inside the personal folder. That leaves a gap: a
brand-new personal repo created with a plain `git init` + `git remote add`
(not `git clone`) never goes through the wrapper, so it silently gets
rewritten to the *work* SSH identity and the guard hooks block both the
commit and the push with a confusing "wrong identity" error. Scoping the
`insteadOf` rule per (host, owner) instead of blanket-to-one-account closes
that gap at the source, makes the shell wrapper unnecessary altogether, and
generalizes cleanly to any number of accounts across any number of hosts.
