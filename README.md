# git-account-setup

Portable setup for keeping multiple git hosting accounts separate on one
machine — any mix of GitHub, GitLab, or self-hosted servers: SSH host
aliases, per-repo identity, owner-scoped URL rewriting, commit/push guard
hooks, and a GitHub CLI (`gh`) that uses the right account on its own. Pure
Python stdlib (tkinter for the GUI is optional) — works on Windows, macOS,
and Linux.

## Requirements

Git (with OpenSSH, which Git for Windows includes) and Python 3.8+, standard
library only — no `pip install` needed, no `requirements.txt`. `gh` is
installed for you if you use that option. The GUI wizard additionally needs
`tkinter`, which ships
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
- **your username** on that host (the account you log in with), plus any
  other orgs/owners that belong to this account
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
    `https://<host>/<owner>/...` remote (or the SSH form
    `git@<host>:<owner>/...`) is rewritten to the right SSH alias
    based on *who owns the repo*, not which folder you happen to be in or
    how the remote was added. This means `git clone`, `git init` +
    `git remote add`, and GUI clients all just work, with no shell wrapper
    needed for git, for GitHub, GitLab, or a self-hosted server alike.
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
  The hooks also give repos owned by *someone else* a push URL — see
  [Repos you don't own](#repos-you-dont-own).
- git aliases per account: `git whoami`, `git use-<label>`,
  `git clone-<label>`.

## Repos you don't own

The `insteadOf` rewriting only knows the owners you listed. A repo owned by
someone else — one you were added to as a collaborator, or the upstream of a
fork — keeps its plain `https://` (or `git@github.com:`) remote, and the
push guard can't tell which account a push over it would go out as.

So the hooks route that repo's **pushes** through the SSH alias of the
account it *commits as* (its dedicated folder's account, else the default):
they set a per-repo `remote.origin.pushurl` such as
`git@work:someone/their-repo.git`. Fetches keep using the original URL, and
nothing changes on the server.

- `post-checkout` does it when you clone, and `pre-commit` on your first
  commit, so the first push normally just works.
- If a remote was added later and you push before committing, `pre-push`
  sets the push URL, stops that one push (it would have gone out over
  `https` with whatever credentials that uses), and asks you to push again.
- `git use-<label>` moves such a push URL to the new account along with the
  author identity.
- A `pushurl` you set yourself is never changed. If the repo's author
  address isn't one of your accounts on that host, the push is blocked with
  the exact commands to pick an account.

## GitHub CLI (`gh`)

For accounts on `github.com` (or `*.ghe.com`), the setup can also take care of
the GitHub CLI (on by default, one checkbox / question). GitLab and other
hosts only get the git setup above, since `gh` doesn't work with them.

- **Installs `gh`** if it isn't there: winget, scoop or choco on Windows,
  Homebrew on macOS, Homebrew or apt/dnf/pacman/zypper on Linux. If none
  works it tells you to get it from https://cli.github.com.
- **Logs `gh` in to every GitHub account** that isn't logged in yet. Each
  login opens `github.com/login/device` in your browser (the GUI shows the
  code and copies it for you). `gh` logs in whichever account the browser is
  signed in to, so sign the browser in to the account being asked for, or use
  a private window. If the wrong account got logged in, the setup says so.
- **Makes plain `gh` use the right account.** A small `gh` wrapper is added to
  your PowerShell profile, `~/.bashrc` (Git Bash on Windows) and `~/.zshrc`
  (macOS). Every `gh` command picks the account, in this order:
  1. an owner named in the command, e.g. `gh repo create your-user/new` or
     `--repo your-company-org/app`
  2. the repo you're in (its `origin`, then the email it commits with)
  3. the dedicated folder you're in, even if it isn't a repo yet
  4. the default account

  It passes that account's token to that one `gh` command only, so terminals
  don't interfere with each other. `gh auth ...` commands are left alone, and
  a `GH_TOKEN` you set yourself always wins. Run `gh-whoami` to see which
  account `gh` would use here, and why. Open a new terminal after the setup so
  the profile change is loaded. If PowerShell's execution policy blocks
  profiles, the setup tells you the command that allows them.
- Sets `gh`'s active account to your default account, which is what `gh` uses
  where the wrapper isn't loaded (e.g. `cmd.exe`).

Git operations never depend on `gh`'s login: clone, pull and push always go
through the SSH aliases above, including clones that `gh` starts.

## Remembering your last answers

On a successful apply, your answers — labels, hosts, names, emails, owners,
folders, key **paths** (never key contents), and whether to set up `gh` — are
cached to
`~/.git-account-setup-cache.json`. Next time you open the wizard, every
account and field is prefilled from that cache, so you can see your last
configuration and just tweak or re-confirm it instead of retyping everything
(`--dry-run` doesn't update the cache).

## Safety

- Every file the script would modify (including shell profiles) is backed up
  first, to `~/.git-identity-backup-<timestamp>/`. Profile edits live in a
  marked block and keep the file's encoding and line endings; unticking the
  `gh` option removes the block again.
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
that gap at the source, makes a shell wrapper unnecessary for git, and
generalizes cleanly to any number of accounts across any number of hosts.

## SSH keys and quick setup on another machine

The tool generates one ed25519 key per account. After applying, the GUI opens an
"Add your SSH keys" window: for each account it shows the public key with a
**Copy key** button, says which host account to sign in to, opens the right
key-upload page, and has a **Re-check** button that tests the connection. The
terminal wizard prints the same steps and keys.

For a no-questions setup, copy `accounts.example.json` to `accounts.json`, edit
it, and run `python setup_git_accounts.py --config accounts.json`
(`~` works in paths). `--from-cache` reuses your last wizard answers.
