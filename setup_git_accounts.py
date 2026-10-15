#!/usr/bin/env python3
"""
git_account_setup.py -- separate multiple Git hosting accounts (GitHub,
GitLab, or any self-hosted host) on one machine.

For each account you configure:
  - an SSH host alias (~/.ssh/config) pointing at that account's git host,
    with a dedicated key and IdentitiesOnly yes
  - a small identity file (~/.gitconfig-<label>) with [user] name/email
  - owner-scoped URL rewriting in ~/.gitconfig, so ANY
    https://<host>/<owner>/... remote -- typed by hand, `git clone`, a GUI
    client, whatever -- is transparently rewritten to the right SSH alias.
    This is scoped per (host, owner) pair, not a single blanket rule, so it
    works correctly no matter which folder you're in or how the remote was
    added -- there is no "new repo created outside a shell wrapper" gap.
  - an optional dedicated folder: repos under it always use that account's
    identity, even before a remote exists. At most one account can also be
    marked "default", used for repos that live outside every dedicated
    folder and have no matching remote yet.
  - `git whoami`, `git use-<label>`, `git clone-<label>` aliases.

For GitHub accounts (github.com or *.ghe.com) it can also set up the GitHub
CLI: install `gh` if it's missing, log `gh` in to every GitHub account, and
add a small `gh` wrapper to your PowerShell / bash / zsh profiles so plain
`gh repo create`, `gh pr create`, ... automatically act as the account that
owns the current repo or folder. `gh-whoami` tells you which one and why.

One pair of hooks (pre-commit, pre-push), wired up via core.hooksPath,
refuses to commit or push under the wrong identity for a given remote --
matching is by host+owner, not tied to GitHub specifically, so GitLab and
self-hosted remotes are guarded exactly the same way. Every other hook name
gets a small pass-through stub so repo-local hooks (husky, lint-staged,
...) still run.

Every write is preceded by a timestamped backup of the files it touches,
and re-running the script is idempotent (it replaces its own previously
written block instead of duplicating it).

On a successful (non-dry-run) apply, your answers -- labels, hosts, names,
emails, owners, folders, key paths; never key contents -- are cached to
~/.git-account-setup-cache.json and used to prefill the wizard next time you
open it, so you can see/tweak your last configuration instead of retyping it.

Usage:
    python setup_git_accounts.py            # GUI wizard (tkinter)
    python setup_git_accounts.py --cli      # terminal wizard, no tkinter needed
    python setup_git_accounts.py --cli --dry-run   # show what would happen, write nothing
    python setup_git_accounts.py --config accounts.json   # no questions (see below)
    python setup_git_accounts.py --from-cache             # reuse the last wizard answers

New laptop in one command: copy ~/.git-account-setup-cache.json (or your own
accounts.json in the same format) over, run with --config, then upload the
public keys it prints. Repos in an account's dedicated folder also use that
account's SSH key by path (core.sshCommand), and owner names are matched in
both original and lowercase spelling.

Works on Windows, macOS and Linux (Python 3.8+, stdlib only; tkinter is
optional and only needed for the GUI wizard).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BEGIN_MARK = "# >>> git-multi-account (managed by git_account_setup.py) >>>"
END_MARK = "# <<< git-multi-account (managed by git_account_setup.py) <<<"

CACHE_FILE = Path.home() / ".git-account-setup-cache.json"

LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

STUB_HOOK_NAMES = [
    "applypatch-msg", "pre-applypatch", "post-applypatch",
    "pre-merge-commit", "prepare-commit-msg", "commit-msg",
    "post-commit", "pre-rebase", "post-merge",
    "pre-auto-gc", "post-rewrite", "push-to-checkout",
    "sendemail-validate",
]

STUB_HOOK_BODY = """\
#!/bin/sh
# Global hook wrapper. core.hooksPath replaces .git/hooks entirely, so hand
# control to the repository's own hook if it has one (husky, lint-staged, ...).
# Note: `git rev-parse --git-path hooks/X` must NOT be used here -- it honours
# core.hooksPath and would resolve back to this very script (infinite loop).
name=$(basename "$0")
gitdir=$(git rev-parse --absolute-git-dir 2>/dev/null) || exit 0
local_hook="$gitdir/hooks/$name"
if [ -x "$local_hook" ] && [ "$local_hook" != "$0" ]; then
    exec "$local_hook" "$@"
fi
exit 0
"""

# Strips a leading https://<host>/, git@<host>:, or ssh://git@<host>/ from a
# remote URL, leaving "<owner>/<repo>[.git]" -- works for any host, not just
# github.com, so GitHub, GitLab and self-hosted remotes are all handled the
# same way.
STRIP_HOST_SED = r"sed -E 's#^(https://[^/]+/|git@[^:]+:|ssh://git@[^/]+/)##'"

GH_BEGIN = "# >>> git-multi-account gh wrapper (managed by setup_git_accounts.py) >>>"
GH_END = "# <<< git-multi-account gh wrapper (managed by setup_git_accounts.py) <<<"

TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def to_posix(p: Path) -> str:
    """git config / ssh config always want forward slashes, even on Windows."""
    return str(p).replace("\\", "/")


def is_github_host(host: str) -> bool:
    host = host.lower()
    return host == "github.com" or host.endswith(".ghe.com")


def gh_env() -> dict:
    # A token in the environment would override every stored gh login.
    return {k: v for k, v in os.environ.items() if k not in TOKEN_ENV_VARS}


def owner_variants(owner: str) -> list[str]:
    """URL owners as typed vs. lowercased. GitHub/GitLab URLs are case-insensitive
    but git's insteadOf / includeIf matching is not, so cover both spellings."""
    out = [owner]
    if owner.lower() != owner:
        out.append(owner.lower())
    return out


def tilde_path(p) -> str:
    """Store paths under $HOME as ~/... so a saved config works on another machine/user."""
    p = Path(p)
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


@dataclass
class Account:
    label: str              # unique short id: SSH host alias & hook/alias names
    name: str                # git author name
    email: str
    host: str                # git hosting domain, e.g. github.com, gitlab.com, git.example.com
    owners: list[str]        # owners[0] is your login username; the rest are extra orgs/owners
    key_path: Path
    generate_key: bool = False
    folder: Path | None = None   # dedicated folder; None = identified by remote only
    is_default: bool = False     # used for repos outside every folder with no matching remote

    @property
    def identity_file(self) -> Path:
        return Path.home() / f".gitconfig-{self.label}"

    @property
    def gh_user(self) -> str:
        return self.owners[0]

    @property
    def is_github(self) -> bool:
        return is_github_host(self.host)


@dataclass
class SetupConfig:
    accounts: list[Account]
    hooks_dir: Path = field(default_factory=lambda: Path.home() / ".githooks")
    home: Path = field(default_factory=Path.home)
    setup_gh: bool = True

    @property
    def default_account(self) -> Account | None:
        for a in self.accounts:
            if a.is_default:
                return a
        return None


def validate_accounts(accounts: list[Account]) -> list[str]:
    errors = []
    if not accounts:
        errors.append("at least one account is required")
    labels_seen = {}
    owner_pairs_seen = {}
    defaults = 0
    for a in accounts:
        if not LABEL_RE.match(a.label or ""):
            errors.append(f"label '{a.label}' must contain only letters, digits, - or _")
        key = a.label.lower()
        if key in labels_seen:
            errors.append(f"label '{a.label}' is used more than once")
        labels_seen[key] = True
        if not a.email:
            errors.append(f"account '{a.label}': email is required")
        if not a.owners or not a.owners[0]:
            errors.append(f"account '{a.label}': at least one owner/username is required")
        for owner in a.owners:
            pair = (a.host.lower(), owner.lower())
            if pair in owner_pairs_seen:
                errors.append(
                    f"'{a.host}/{owner}' is claimed by both '{owner_pairs_seen[pair]}' and '{a.label}'"
                )
            owner_pairs_seen[pair] = a.label
        if a.is_default:
            defaults += 1
    if defaults > 1:
        errors.append("only one account can be the default")
    return errors


LogFn = "callable"


def run(cmd: list[str], log) -> None:
    log("  $ " + " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def backup_existing(home: Path, paths: list[Path], log) -> Path | None:
    existing = [p for p in paths if p.exists()]
    if not existing:
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = home / f".git-identity-backup-{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for p in existing:
        dest = backup_dir / p.name
        if dest.exists():  # e.g. two PowerShell profiles both named profile.ps1
            dest = backup_dir / f"{p.parent.name}-{p.name}"
        shutil.copy2(p, dest)
    log(f"backed up {len(existing)} existing file(s) to {backup_dir}")
    return backup_dir


# --------------------------------------------------------------------------
# cache -- remembers the last answers you gave (never key contents) so the
# wizard opens pre-filled with what you last set up
# --------------------------------------------------------------------------

def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def account_to_cache(acct: Account) -> dict:
    return {
        "label": acct.label,
        "name": acct.name,
        "email": acct.email,
        "host": acct.host,
        "owners": acct.owners,
        "key_path": tilde_path(acct.key_path),
        "generate_key": acct.generate_key,
        "folder": tilde_path(acct.folder) if acct.folder else "",
        "is_default": acct.is_default,
    }


def save_cache(cfg: SetupConfig) -> None:
    data = {
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "hooks_dir": tilde_path(cfg.hooks_dir),
        "setup_gh": cfg.setup_gh,
        "accounts": [account_to_cache(a) for a in cfg.accounts],
    }
    try:
        CACHE_FILE.write_text(json.dumps(data, indent=2), newline="\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# SSH
# --------------------------------------------------------------------------

def ensure_ssh_key(acct: Account, dry_run: bool, log) -> None:
    if not acct.generate_key:
        if not acct.key_path.exists():
            log(f"  WARNING: key {acct.key_path} does not exist and 'generate' was off -- "
                f"ssh will fail for {acct.label} until you create it")
        return
    if acct.key_path.exists():
        log(f"  key already exists, reusing: {acct.key_path}")
        return
    log(f"  generating new ed25519 key for {acct.label}: {acct.key_path}")
    if dry_run:
        return
    acct.key_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-C", acct.email,
         "-f", str(acct.key_path), "-N", ""],
        check=True,
    )
    try:
        os.chmod(acct.key_path, 0o600)
    except OSError:
        pass


def write_ssh_config(cfg: SetupConfig, dry_run: bool, log) -> None:
    ssh_dir = cfg.home / ".ssh"
    ssh_cfg_path = ssh_dir / "config"
    existing = ssh_cfg_path.read_text() if ssh_cfg_path.exists() else ""

    new_blocks = []
    for acct in cfg.accounts:
        if f"Host {acct.label}\n" in existing or existing.strip() == f"Host {acct.label}":
            log(f"  ~/.ssh/config already has 'Host {acct.label}' -- leaving it untouched")
            continue
        new_blocks.append(
            f"Host {acct.label}\n"
            f"    HostName {acct.host}\n"
            f"    User git\n"
            f"    IdentityFile {to_posix(acct.key_path)}\n"
            f"    IdentitiesOnly yes\n"
        )

    if not new_blocks:
        return

    log(f"  appending {len(new_blocks)} Host block(s) to {ssh_cfg_path}")
    if dry_run:
        for b in new_blocks:
            log(textwrap.indent(b, "    "))
        return

    ssh_dir.mkdir(exist_ok=True)
    with ssh_cfg_path.open("a", newline="\n") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        if existing:
            f.write("\n")
        f.write("\n".join(new_blocks))


# --------------------------------------------------------------------------
# gitconfig
# --------------------------------------------------------------------------

def write_identity_file(acct: Account, dry_run: bool, log) -> None:
    content = (
        f"# {acct.label} identity ({acct.host}) -- managed by git_account_setup.py\n"
        f"[user]\n"
        f"\tname = {acct.name}\n"
        f"\temail = {acct.email}\n"
        f"\n"
        f'[credential "https://{acct.host}"]\n'
        f"\tusername = {acct.owners[0]}\n"
    )
    if acct.folder:
        # Path-based SSH key: any repo inside the account's folder (even one cloned
        # from an owner/org git does not know about) authenticates with this key.
        content += (
            f"\n[core]\n"
            f'\tsshCommand = ssh -i "{to_posix(acct.key_path.expanduser())}" -o IdentitiesOnly=yes\n'
        )
    log(f"  writing {acct.identity_file}")
    if not dry_run:
        acct.identity_file.write_text(content, newline="\n")


def build_managed_block(cfg: SetupConfig) -> str:
    lines = [BEGIN_MARK]
    for a in cfg.accounts:
        tag = " [default]" if a.is_default else ""
        folder_note = f" folder={to_posix(a.folder)}" if a.folder else ""
        lines.append(f"# {a.label}: {a.host}/{', '.join(a.owners)} <{a.email}>{folder_note}{tag}")

    # Default identity. Git config is "last value in file order wins", and an
    # includeIf's content is spliced in at the point the includeIf directive
    # appears -- so this [user] block MUST come before the includeIf rules
    # below, or the default would always win over any conditional match.
    default_acct = cfg.default_account
    if default_acct is not None:
        lines.append("[user]")
        lines.append(f"\tname = {default_acct.name}")
        lines.append(f"\temail = {default_acct.email}")

    # Owner-scoped URL rewriting: any https://<host>/<owner>/... remote is
    # rewritten to the right SSH alias, regardless of current folder or how
    # the remote was added (init+remote add, clone, GUI client, ...).
    # insteadOf replaces the whole matched prefix, so the owner must be
    # repeated in the replacement or it is dropped from the URL. The SSH
    # forms cover the "SSH" clone URLs GitHub/GitLab show, and make gh's
    # git_protocol setting irrelevant.
    for a in cfg.accounts:
        for owner in a.owners:
            lines.append(f'[url "git@{a.label}:{owner}/"]')
            for o in owner_variants(owner):
                lines.append(f"\tinsteadOf = https://{a.host}/{o}/")
                lines.append(f"\tinsteadOf = git@{a.host}:{o}/")
                lines.append(f"\tinsteadOf = ssh://git@{a.host}/{o}/")

    # Folder rules: a designated folder always uses that account's identity,
    # even before a remote exists (e.g. `git init` + first commit).
    for a in cfg.accounts:
        if not a.folder:
            continue
        folder = to_posix(a.folder)
        if not folder.endswith("/"):
            folder += "/"
        lines.append(f'[includeIf "gitdir/i:{folder}"]')
        lines.append(f"\tpath = {to_posix(a.identity_file)}")

    # Remote-based rules: whichever identity matches the actual remote wins,
    # so an account's repo living outside its folder (or with no folder at
    # all) still gets the right author once a remote is attached.
    for a in cfg.accounts:
        for pattern in (
            f"git@{a.label}:**/**",
            f"ssh://git@{a.label}/**",
        ):
            lines.append(f'[includeIf "hasconfig:remote.*.url:{pattern}"]')
            lines.append(f"\tpath = {to_posix(a.identity_file)}")
        for owner in a.owners:
            for pattern in (
                p for o in owner_variants(owner) for p in (
                    f"https://{a.host}/{o}/**",
                    f"git@{a.host}:{o}/**",
                    f"ssh://git@{a.host}/{o}/**",
                )
            ):
                lines.append(f'[includeIf "hasconfig:remote.*.url:{pattern}"]')
                lines.append(f"\tpath = {to_posix(a.identity_file)}")

    lines.append(END_MARK)
    return "\n".join(lines) + "\n"


def strip_block(content: str, begin: str, end: str) -> str:
    if begin not in content:
        return content
    pre, _, rest = content.partition(begin)
    _, _, post = rest.partition(end)
    return pre.rstrip("\n") + ("\n" if pre.strip() else "") + post.lstrip("\n")


def strip_managed_block(content: str) -> str:
    return strip_block(content, BEGIN_MARK, END_MARK)


def read_text_keep_format(path: Path) -> tuple[str, str, str]:
    """(text with \\n newlines, encoding, newline) so a rewrite keeps the file's BOM and line endings."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "latin-1"  # decodes any bytes and writes them back unchanged
        text = raw.decode(encoding)
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), encoding, newline


def upsert_block(path: Path, begin: str, end: str, block: str | None, dry_run: bool, log) -> None:
    """Replace (or, with block=None, remove) a marked block in a text file, leaving the rest untouched."""
    if not path.exists() and block is None:
        return
    text, encoding, newline = read_text_keep_format(path) if path.exists() else ("", "utf-8", "\n")
    kept = strip_block(text, begin, end)
    if block is None:
        new = kept
    else:
        new = kept.rstrip("\n") + ("\n\n" if kept.strip() else "") + block
    if new == text:
        return
    log(f"  {'updating' if block else 'removing gh wrapper from'} {path}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(new)


def whoami_alias(cfg: SetupConfig) -> str:
    cases = "".join(
        f"{a.email}) echo '{a.label.upper()} ({a.host}/{a.owners[0]})';; "
        for a in cfg.accounts
    )
    return (
        "!f() { printf 'account : %s\\nname    : %s\\nemail   : %s\\nremote  : %s\\n' "
        f'"$(case "$(git config user.email)" in {cases}*) echo UNKNOWN;; esac)" '
        '"$(git config user.name)" "$(git config user.email)" '
        "\"$(git remote get-url origin 2>/dev/null || echo '<none>')\"; }; f"
    )


def use_alias(acct: Account) -> str:
    return (
        f'!f() {{ git config user.name "{acct.name}" '
        f'&& git config user.email "{acct.email}" '
        f'&& echo "this repo -> {acct.label.upper()} ({acct.host}/{acct.owners[0]})"; }}; f'
    )


def clone_alias(label: str) -> str:
    return (
        f"!f() {{ r=$(printf '%s' \"$1\" | {STRIP_HOST_SED}); r=${{r%.git}}; shift; "
        f'git clone "git@{label}:$r.git" "$@"; }}; f'
    )


def update_global_gitconfig(cfg: SetupConfig, dry_run: bool, log) -> None:
    gitconfig_path = cfg.home / ".gitconfig"
    existing = gitconfig_path.read_text() if gitconfig_path.exists() else ""
    existing = strip_managed_block(existing)
    block = build_managed_block(cfg)
    new_content = existing.rstrip("\n")
    new_content += ("\n\n" if new_content.strip() else "") + block

    log(f"  updating {gitconfig_path}")
    if dry_run:
        log(textwrap.indent(block, "    "))
    else:
        gitconfig_path.write_text(new_content, newline="\n")

    def git_config(args: list[str]) -> None:
        if dry_run:
            log("  $ git config --global " + " ".join(args))
            return
        subprocess.run(["git", "config", "--global"] + args, check=True)

    git_config(["core.hooksPath", to_posix(cfg.hooks_dir)])

    result = subprocess.run(
        ["git", "config", "--global", "--get", "init.defaultBranch"],
        capture_output=True,
    )
    if result.returncode != 0:
        git_config(["init.defaultBranch", "main"])

    git_config(["alias.whoami", whoami_alias(cfg)])
    for a in cfg.accounts:
        git_config([f"alias.use-{a.label}", use_alias(a)])
        git_config([f"alias.clone-{a.label}", clone_alias(a.label)])


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

def build_pre_commit(cfg: SetupConfig) -> str:
    cases = "\n".join(
        f'    git@{a.label}:*|ssh://git@{a.label}/*|{a.label}:*)\n'
        f"        expected=\"{a.email}\"; label='{a.label.upper()} "
        f"({a.host}/{a.owners[0]})'; fix='git use-{a.label}' ;;"
        for a in cfg.accounts
    )
    return f"""\
#!/bin/sh
# Identity guard, commit-time. Blocks a commit whose author address does not
# match the account this repo's origin belongs to (matched by host alias, so
# this works the same for GitHub, GitLab, or any self-hosted remote).
url=$(git remote get-url origin 2>/dev/null)
case "$url" in
{cases}
    *)  expected='' ;;
esac

if [ -n "$expected" ]; then
    configured=$(git config user.email 2>/dev/null)
    if [ "$configured" != "$expected" ]; then
        cat >&2 <<MSG

  COMMIT BLOCKED -- wrong identity for this repository

    origin belongs to : $label
    expected author   : $expected
    currently set to  : ${{configured:-<none>}}

  Fix with:  $fix
  Override:  git commit --no-verify

MSG
        exit 1
    fi
fi

gitdir=$(git rev-parse --absolute-git-dir 2>/dev/null) || exit 0
local_hook="$gitdir/hooks/pre-commit"
if [ -x "$local_hook" ] && [ "$local_hook" != "$0" ]; then exec "$local_hook" "$@"; fi
exit 0
"""


def build_pre_push(cfg: SetupConfig) -> str:
    cases = []
    for acct in cfg.accounts:
        forbidden_emails = " ".join(a.email for a in cfg.accounts if a.label != acct.label)
        cases.append(
            f'    git@{acct.label}:*|ssh://git@{acct.label}/*|{acct.label}:*)\n'
            f'        account="{acct.label.upper()} ({acct.host}/{acct.owners[0]})"; '
            f'expected="{acct.email}"; forbidden_emails="{forbidden_emails}" ;;'
        )
    cases_str = "\n".join(cases)
    alias_hints = "\n".join(f"    git remote set-url $remote_name git@{a.label}:<owner>/<repo>.git" for a in cfg.accounts)
    fix_hints = "   or   ".join(f"git use-{a.label}" for a in cfg.accounts)
    return f"""\
#!/bin/sh
# Identity guard, push-time. Refuses to send commits authored with the wrong
# account's address for this remote. Matching is by host alias, so this
# works the same for GitHub, GitLab, or any self-hosted remote.
remote_name="$1"
remote_url="$2"
input=$(cat)

chain_local () {{
    gitdir=$(git rev-parse --absolute-git-dir 2>/dev/null) || exit 0
    local_hook="$gitdir/hooks/pre-push"
    if [ -x "$local_hook" ] && [ "$local_hook" != "$0" ]; then
        printf '%s\\n' "$input" | "$local_hook" "$remote_name" "$remote_url"
        exit $?
    fi
    exit 0
}}

case "$remote_url" in
{cases_str}
    https://*|git@*:*|ssh://git@*/*)
        cat >&2 <<MSG

  PUSH BLOCKED -- unrecognized remote

    remote: $remote_url

  This URL doesn't match any configured account's host alias, so the guard
  can't check it. Point it at a host alias instead, e.g.:

{alias_hints}

  Override (not recommended):  git push --no-verify

MSG
        exit 1 ;;
    *)  chain_local ;;
esac

configured=$(git config user.email 2>/dev/null)
if [ "$configured" != "$expected" ]; then
    cat >&2 <<MSG

  PUSH BLOCKED -- wrong identity configured

    remote belongs to : $account
    expected author   : $expected
    currently set to  : ${{configured:-<none>}}

  Fix with:  {fix_hints}

MSG
    exit 1
fi

zero_sha () {{ case "$1" in *[!0]*) return 1 ;; *) return 0 ;; esac; }}
offenders=$(mktemp) || exit 1
trap 'rm -f "$offenders"' EXIT

printf '%s\\n' "$input" | while read -r local_ref local_sha remote_ref remote_sha; do
    [ -z "$local_sha" ] && continue
    zero_sha "$local_sha" && continue
    if zero_sha "$remote_sha"; then
        set -- "$local_sha" --not --remotes="$remote_name"
    else
        set -- "$remote_sha..$local_sha"
    fi
    for femail in $forbidden_emails; do
        git log "$@" --author="$femail" --format='%h  %an <%ae>  %s' 2>/dev/null >> "$offenders"
    done
done

if [ -s "$offenders" ]; then
    cat >&2 <<MSG

  PUSH BLOCKED -- commits authored with another account

    remote belongs to : $account
    but these commits were authored under a different account:

MSG
    sed 's/^/    /' "$offenders" >&2
    cat >&2 <<MSG

  Pushing these would expose the other account in this repository's history.
  To re-author every commit on this branch:

    git rebase -r --root --exec 'git commit --amend --no-edit --reset-author'

  Override (not recommended):  git push --no-verify

MSG
    exit 1
fi

chain_local
"""


def build_post_checkout(cfg: SetupConfig) -> str:
    folder_accounts = [a for a in cfg.accounts if a.folder]
    case_lines = []
    for a in folder_accounts:
        folder_lower = str(a.folder).replace("\\", "/").lower()
        case_lines.append(f'    {folder_lower}|{folder_lower}/*) retarget "{a.label}" ;;')
    cases_str = "\n".join(case_lines) if case_lines else "    *) : ;;"
    return f"""\
#!/bin/sh
# A repo cloned into a designated folder should talk to its git host as that
# folder's account, so retarget a bare (not-yet-aliased) origin at the right
# SSH alias (belt-and-suspenders: the owner-scoped insteadOf rule in
# ~/.gitconfig should already have rewritten it before clone). Works for any
# host (GitHub, GitLab, self-hosted) since the strip below is host-agnostic.
retarget () {{
    url=$(git remote get-url origin 2>/dev/null)
    case "$url" in
        https://*|git@*:*|ssh://git@*/*)
            repo=$(printf '%s' "$url" | {STRIP_HOST_SED})
            git remote set-url origin "git@$1:$repo"
            echo "  note: origin retargeted -> git@$1:$repo" >&2 ;;
    esac
}}

top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
lower=$(printf '%s' "$top" | tr 'A-Z' 'a-z')
case "$lower" in
{cases_str}
esac

gitdir=$(git rev-parse --absolute-git-dir 2>/dev/null) || exit 0
local_hook="$gitdir/hooks/post-checkout"
if [ -x "$local_hook" ] && [ "$local_hook" != "$0" ]; then exec "$local_hook" "$@"; fi
exit 0
"""


def write_hooks(cfg: SetupConfig, dry_run: bool, log) -> None:
    log(f"  writing hooks to {cfg.hooks_dir}")
    hooks = {
        "pre-commit": build_pre_commit(cfg),
        "pre-push": build_pre_push(cfg),
        "post-checkout": build_post_checkout(cfg),
    }
    for name in STUB_HOOK_NAMES:
        hooks[name] = STUB_HOOK_BODY

    if dry_run:
        log(f"    ({len(hooks)} hook files)")
        return

    cfg.hooks_dir.mkdir(parents=True, exist_ok=True)
    for name, content in hooks.items():
        path = cfg.hooks_dir / name
        path.write_text(content, newline="\n")
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


# --------------------------------------------------------------------------
# GitHub CLI (gh)
# --------------------------------------------------------------------------

RESOLVER_TEMPLATE = r'''#!/usr/bin/env python3
# Generated by setup_git_accounts.py -- re-run the setup to change it.
#
#   gh_account.py [gh args...]           print the gh token of the account that
#                                        owns this context (nothing if none)
#   gh_account.py --explain [gh args...] say which account would be used, and why
#
# Order: an owner named in the gh arguments (owner/repo or a URL), then the
# current repo (its origin, then the email git commits with), then the
# dedicated folder you're in, then the default account.
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ACCOUNTS = json.loads(__ACCOUNTS__)
GH = __GH__
if not os.path.exists(GH):
    GH = shutil.which("gh") or "gh"

TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
OWNER_REPO = re.compile(
    r"^(?:https?://([^/]+)/|git@([^:/]+):|ssh://git@([^/]+)/)?([A-Za-z0-9][A-Za-z0-9._-]*)/[^/\s]+"
)


def run(cmd):
    env = {k: v for k, v in os.environ.items() if k not in TOKEN_ENV_VARS}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def by_owner(tokens):
    for tok in tokens:
        if tok.startswith("-") and "=" in tok:
            tok = tok.split("=", 1)[1]
        m = OWNER_REPO.match(tok)
        if not m:
            continue
        host = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        owner = m.group(4).lower()
        for a in ACCOUNTS:
            if host:
                if host not in (a["host"].lower(), a["label"].lower()):
                    continue
            elif not a["github"]:
                continue
            if owner in (o.lower() for o in a["owners"]):
                return a, "'%s' belongs to %s/%s" % (tok, a["host"], owner)
    return None


def by_repo():
    if run(["git", "rev-parse", "--is-inside-work-tree"]) != "true":
        return None
    url = run(["git", "remote", "get-url", "origin"])
    if url:
        found = by_owner([url])
        if found:
            return found[0], "this repo's origin is %s" % url
    email = run(["git", "config", "user.email"])
    if email:
        for a in ACCOUNTS:
            if a["email"].lower() == email.lower():
                return a, "this repo commits as %s" % email
    return None


def by_folder():
    here = os.path.normcase(str(Path.cwd().resolve()))
    best = None
    for a in ACCOUNTS:
        if not a["folder"]:
            continue
        folder = os.path.normcase(str(Path(a["folder"]).resolve())).rstrip("\\/")
        if (here == folder or here.startswith(folder + os.sep)) and (best is None or len(folder) > best[1]):
            best = (a, len(folder))
    if best:
        return best[0], "you're inside its folder %s" % best[0]["folder"]
    return None


def by_default():
    for a in ACCOUNTS:
        if a["is_default"]:
            return a, "it's the default account"
    return None


def main():
    args = sys.argv[1:]
    explain = args[:1] == ["--explain"]
    if explain:
        args = args[1:]
    elif args[:1] == ["auth"]:
        return  # gh refuses `gh auth ...` while GH_TOKEN is set
    found = by_owner(args) or by_repo() or by_folder() or by_default()
    if not found:
        if explain:
            print("no configured account matches here -- gh uses its active account")
        return
    a, why = found
    if not a["github"]:
        if explain:
            print("%s is on %s, not GitHub (%s) -- gh uses its active account" % (a["label"], a["host"], why))
        return
    token = run([GH, "auth", "token", "--hostname", a["host"], "--user", a["user"]])
    if explain:
        print("account : %s (%s/%s)" % (a["label"], a["host"], a["user"]))
        print("reason  : %s" % why)
        print("gh login: %s" % ("ok" if token else "NOT logged in -- run: gh auth login --hostname " + a["host"]))
    elif token:
        print(token)
    else:
        sys.stderr.write("gh: %s isn't logged in on %s -- run: gh auth login --hostname %s\n"
                         % (a["user"], a["host"], a["host"]))


if __name__ == "__main__":
    main()
'''


def find_gh() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    candidates = []
    if os.name == "nt":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates += [Path(base) / "GitHub CLI" / "gh.exe", Path(base) / "Programs" / "GitHub CLI" / "gh.exe"]
        candidates.append(Path.home() / "scoop" / "shims" / "gh.exe")
    else:
        candidates += [Path("/opt/homebrew/bin/gh"), Path("/usr/local/bin/gh"),
                       Path("/home/linuxbrew/.linuxbrew/bin/gh")]
    return next((str(c) for c in candidates if c.exists()), None)


def gh_install_commands(interactive: bool) -> list[list[str]]:
    sudo = ["sudo"] if interactive else ["sudo", "-n"]  # -n: fail instead of prompting when there's no terminal
    if os.name == "nt":
        commands = [
            ["winget", "install", "--id", "GitHub.cli", "-e", "--silent",
             "--accept-source-agreements", "--accept-package-agreements"],
            ["scoop", "install", "gh"],
            ["choco", "install", "gh", "-y"],
        ]
    elif sys.platform == "darwin":
        commands = [["brew", "install", "gh"]]
    else:
        commands = [
            ["brew", "install", "gh"],
            sudo + ["apt-get", "install", "-y", "gh"],
            sudo + ["dnf", "install", "-y", "gh"],
            sudo + ["pacman", "-S", "--noconfirm", "github-cli"],
            sudo + ["zypper", "--non-interactive", "install", "gh"],
        ]
    available = []
    for cmd in commands:
        tools = [cmd[0]] + ([next(x for x in cmd[1:] if x != "-n")] if cmd[0] == "sudo" else [])
        if all(shutil.which(t) for t in tools):
            available.append(cmd)
    return available


def run_waiting(cmd: list[str], pump, interactive: bool) -> int:
    proc = subprocess.Popen(cmd, stdin=None if interactive else subprocess.DEVNULL)
    while proc.poll() is None:
        pump()
        time.sleep(0.1)
    return proc.returncode


def install_gh(interactive: bool, dry_run: bool, pump, log) -> str | None:
    commands = gh_install_commands(interactive)
    if not commands:
        log("  no supported package manager found -- install gh from https://cli.github.com, then re-run")
        return None
    for cmd in commands:
        log("  $ " + " ".join(cmd))
        if dry_run:
            return None
        try:
            code = run_waiting(cmd, pump, interactive)
        except OSError as e:
            code = str(e)
        gh = find_gh()
        if gh:
            log(f"  installed gh: {gh}")
            return gh
        log(f"    didn't work (exit {code}), trying the next option")
    log("  couldn't install gh automatically -- install it from https://cli.github.com, then re-run")
    return None


def gh_logged_in(gh: str, host: str, user: str) -> bool:
    r = subprocess.run([gh, "auth", "token", "--hostname", host, "--user", user],
                       capture_output=True, text=True, env=gh_env())
    return r.returncode == 0 and bool(r.stdout.strip())


def gh_active_user(gh: str, host: str) -> str | None:
    r = subprocess.run([gh, "api", "--hostname", host, "user", "--jq", ".login"],
                       capture_output=True, text=True, env=gh_env())
    return r.stdout.strip() if r.returncode == 0 else None


def gh_login_command(gh: str, acct: Account) -> list[str]:
    return [gh, "auth", "login", "--hostname", acct.host, "--git-protocol", "https", "--web", "--skip-ssh-key"]


def gh_login_terminal(gh: str, acct: Account) -> None:
    print(f"\n  Logging gh in to {acct.host} as {acct.gh_user}.")
    print(f"  Before entering the code, make sure your browser is signed in to {acct.host} as "
          f"{acct.gh_user} (or use a private window) -- gh logs in whichever account the browser uses.\n")
    subprocess.run(gh_login_command(gh, acct), env=gh_env())


def gh_resolver_path(home: Path) -> Path:
    return home / ".git-account-setup" / "gh_account.py"


def resolver_python() -> str:
    # In a venv, point at the base interpreter so the wrapper survives the venv being deleted.
    return getattr(sys, "_base_executable", None) or sys.executable


def write_gh_resolver(cfg: SetupConfig, gh: str, dry_run: bool, log) -> Path:
    data = [
        {
            "label": a.label, "host": a.host, "user": a.gh_user, "owners": a.owners, "email": a.email,
            "folder": str(a.folder) if a.folder else "", "is_default": a.is_default, "github": a.is_github,
        }
        for a in cfg.accounts
    ]
    content = RESOLVER_TEMPLATE.replace("__ACCOUNTS__", repr(json.dumps(data))).replace("__GH__", repr(gh))
    path = gh_resolver_path(cfg.home)
    log(f"  writing {path}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, newline="\n")
    return path


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def sh_gh_wrapper(python: str, resolver: str) -> str:
    py, res = sh_quote(python), sh_quote(resolver)
    return f"""{GH_BEGIN}
# `gh` acts as the account that owns the current repo/folder; `gh-whoami` says which.
gh() {{
    if [ -z "${{GH_TOKEN:-}}" ] && [ -f {res} ] && [ -f {py} ]; then
        local _gma_token
        _gma_token=$({py} {res} "$@")
        if [ -n "$_gma_token" ]; then
            GH_TOKEN="$_gma_token" command gh "$@"
            return
        fi
    fi
    command gh "$@"
}}
gh-whoami() {{ {py} {res} --explain "$@"; }}
{GH_END}
"""


def ps_gh_wrapper(python: str, resolver: str) -> str:
    py, res = ps_quote(python), ps_quote(resolver)
    return f"""{GH_BEGIN}
# `gh` acts as the account that owns the current repo/folder; `gh-whoami` says which.
function gh {{
    $ghExe = Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ghExe) {{ Write-Error 'gh is not installed'; return }}
    if (-not $env:GH_TOKEN -and (Test-Path -LiteralPath {res}) -and (Test-Path -LiteralPath {py})) {{
        $token = & {py} {res} @args
        if ($token) {{
            $env:GH_TOKEN = $token
            try {{ & $ghExe.Source @args }} finally {{ Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue }}
            return
        }}
    }}
    & $ghExe.Source @args
}}
function gh-whoami {{ & {py} {res} --explain @args }}
{GH_END}
"""


@lru_cache(maxsize=None)
def powershell_profile_paths() -> tuple[Path, ...]:
    paths = []
    for exe in ("powershell", "pwsh"):
        if not shutil.which(exe):
            continue
        try:
            r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", "$PROFILE.CurrentUserAllHosts"],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        p = r.stdout.strip()
        if r.returncode == 0 and p and Path(p) not in paths:
            paths.append(Path(p))
    return tuple(paths)


def shell_profile_targets(home: Path) -> list[tuple[Path, str]]:
    targets = []
    bashrc, zshrc = home / ".bashrc", home / ".zshrc"
    shell = os.environ.get("SHELL", "")
    if os.name == "nt" or sys.platform.startswith("linux") or bashrc.exists() or shell.endswith("bash"):
        targets.append((bashrc, "sh"))  # on Windows this is Git Bash
    if sys.platform == "darwin" or zshrc.exists() or shell.endswith("zsh"):
        targets.append((zshrc, "sh"))
    targets += [(p, "ps") for p in powershell_profile_paths()]
    return targets


def write_gh_wrappers(cfg: SetupConfig, resolver: Path | None, dry_run: bool, log) -> None:
    python = resolver_python()
    for path, kind in shell_profile_targets(cfg.home):
        if resolver is None:
            block = None
        elif kind == "sh":
            block = sh_gh_wrapper(to_posix(Path(python)), to_posix(resolver))
        else:
            block = ps_gh_wrapper(python, str(resolver))
        upsert_block(path, GH_BEGIN, GH_END, block, dry_run, log)


def warn_powershell_policy(log) -> None:
    if os.name != "nt" or not shutil.which("powershell"):
        return
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
                       capture_output=True, text=True)
    policy = r.stdout.strip()
    if policy in ("Restricted", "AllSigned"):
        log(f"  NOTE: PowerShell's execution policy is {policy}, so profiles (and the gh wrapper) don't load. "
            "To allow them: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned")


def setup_gh(cfg: SetupConfig, dry_run: bool, log, gh_login, pump, interactive: bool) -> None:
    gh_accounts = [a for a in cfg.accounts if a.is_github]
    if not cfg.setup_gh or not gh_accounts:
        log("  skipped (" + ("turned off" if not cfg.setup_gh else "no GitHub accounts") + ")")
        write_gh_wrappers(cfg, None, dry_run, log)
        return

    gh = find_gh()
    if gh:
        log(f"  found gh: {gh}")
    else:
        log("  gh isn't installed -- installing it")
        gh = install_gh(interactive, dry_run, pump, log)
        if not gh:
            return

    for a in gh_accounts:
        if gh_logged_in(gh, a.host, a.gh_user):
            log(f"  {a.label}: gh is logged in as {a.gh_user} on {a.host}")
            continue
        if dry_run:
            log(f"  {a.label}: would log gh in as {a.gh_user} on {a.host}")
            continue
        log(f"  {a.label}: logging gh in as {a.gh_user} on {a.host} (browser sign-in)")
        gh_login(gh, a)
        if gh_logged_in(gh, a.host, a.gh_user):
            log(f"    logged in as {a.gh_user}")
            continue
        who = gh_active_user(gh, a.host)
        if who and who.lower() != a.gh_user.lower():
            log(f"    WARNING: the browser was signed in as {who}, so gh logged in {who} instead of "
                f"{a.gh_user}. Sign the browser in as {a.gh_user} (or use a private window) and re-run.")
        else:
            log(f"    WARNING: the login for {a.gh_user} didn't complete -- re-run the setup to try again.")

    for host in sorted({a.host for a in gh_accounts}):
        log(f"  $ gh config set git_protocol https --host {host}")
        if not dry_run:
            subprocess.run([gh, "config", "set", "git_protocol", "https", "--host", host],
                           capture_output=True, env=gh_env())

    default = cfg.default_account
    if default and default.is_github and not dry_run and gh_logged_in(gh, default.host, default.gh_user):
        subprocess.run([gh, "auth", "switch", "--hostname", default.host, "--user", default.gh_user],
                       capture_output=True, env=gh_env())
        log(f"  gh's active account on {default.host}: {default.gh_user} (used where the wrapper isn't loaded)")

    resolver = write_gh_resolver(cfg, gh, dry_run, log)
    write_gh_wrappers(cfg, resolver, dry_run, log)
    warn_powershell_policy(log)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def verify_accounts(cfg: SetupConfig, log) -> None:
    """ssh -T each alias: show the public key to upload if auth fails, and warn when
    the host reports a login that differs from the configured owner."""
    for a in cfg.accounts:
        try:
            r = subprocess.run(
                ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=10", f"git@{a.label}"],
                capture_output=True, text=True, timeout=30,
            )
            out = (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as e:
            log(f"  {a.label}: could not run ssh ({e})")
            continue
        m = re.search(r"Hi ([^!]+)!|Welcome to GitLab, @(\S+?)!", out)
        if m:
            login = m.group(1) or m.group(2)
            if login.lower() == a.owners[0].lower():
                log(f"  {a.label}: OK (authenticated as {login})")
            else:
                log(f"  {a.label}: WARNING authenticated as '{login}' but owner is '{a.owners[0]}' "
                    f"-- re-run with the correct username or this key is on the wrong account")
            continue
        log(f"  {a.label}: NOT authenticated yet. Add this public key to the {a.host} account "
            f"'{a.owners[0]}' (Settings > SSH and GPG keys), then re-run to verify:")
        pub = Path(str(a.key_path) + ".pub")
        if pub.exists():
            log(f"    {pub.read_text().strip()}")
        if is_github_host(a.host):
            log(f"    https://{a.host}/settings/ssh/new")


def apply_config(cfg: SetupConfig, dry_run: bool, log, gh_login=None, pump=lambda: None,
                 interactive: bool = True) -> None:
    errors = validate_accounts(cfg.accounts)
    if errors:
        for e in errors:
            log(f"  ERROR: {e}")
        raise ValueError("invalid configuration:\n" + "\n".join(errors))

    # git matches gitdir rules against the repo's real path, so expand ~, 8.3
    # short names (C:\Users\ABCDEF~1) and symlinks, or the folder rule never fires.
    for a in cfg.accounts:
        a.key_path = a.key_path.expanduser()
        if a.folder:
            a.folder = a.folder.expanduser().resolve()
    cfg.hooks_dir = cfg.hooks_dir.expanduser()

    log("== backing up existing files ==")
    to_backup = [cfg.home / ".gitconfig", cfg.home / ".ssh" / "config"]
    to_backup += [a.identity_file for a in cfg.accounts]
    to_backup += [path for path, _ in shell_profile_targets(cfg.home)]
    if not dry_run:
        backup_existing(cfg.home, to_backup, log)
    else:
        log("  (skipped: --dry-run)")

    log("== SSH keys ==")
    for a in cfg.accounts:
        ensure_ssh_key(a, dry_run, log)

    log("== SSH config ==")
    write_ssh_config(cfg, dry_run, log)

    log("== identity files ==")
    for a in cfg.accounts:
        write_identity_file(a, dry_run, log)

    log("== global .gitconfig ==")
    update_global_gitconfig(cfg, dry_run, log)

    log("== hooks ==")
    write_hooks(cfg, dry_run, log)

    log("== GitHub CLI (gh) ==")
    try:
        setup_gh(cfg, dry_run, log, gh_login or gh_login_terminal, pump, interactive)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"  WARNING: gh setup failed ({e}). Git itself is fully configured; re-run to retry gh.")

    if not dry_run:
        save_cache(cfg)
        log(f"  remembered these settings in {CACHE_FILE} for next time")

    if not dry_run:
        log("== verifying SSH access ==")
        verify_accounts(cfg, log)

    log("")
    log("Done." if not dry_run else "Dry run complete -- nothing was written.")
    log("")
    log("Verify with, from any repo:")
    log("  git whoami")
    for a in cfg.accounts:
        log(f"  ssh -T git@{a.label}")
    if cfg.setup_gh and any(a.is_github for a in cfg.accounts):
        log("  gh-whoami        (in a new terminal, so the profile change is loaded)")


# --------------------------------------------------------------------------
# CLI wizard
# --------------------------------------------------------------------------

def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{text}{suffix}: ").strip()
    return val or default


def prompt_required(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{text}{suffix}: ").strip() or default
        if val:
            return val
        print("  this field is required.")


def prompt_yn(text: str, default_yes: bool) -> bool:
    default = "Y/n" if default_yes else "y/N"
    val = input(f"{text} ({default}): ").strip().lower()
    if not val:
        return default_yes
    return val.startswith("y")


def cli_wizard() -> SetupConfig:
    print("Git multi-account setup -- terminal wizard\n")

    home = Path.home()
    cache = load_cache()
    cached_accounts = cache.get("accounts", [])
    if cache:
        print(f"(prefilled from {CACHE_FILE}, saved {cache.get('saved_at', 'earlier')} "
              "-- press Enter to keep a value, or type a new one)")

    print("Add each account you want to keep separate on this machine "
          "(GitHub, GitLab, or a self-hosted git server).\n")

    accounts: list[Account] = []
    used_labels: set[str] = set()
    i = 0
    while True:
        c = cached_accounts[i] if i < len(cached_accounts) else {}
        print(f"-- Account {i + 1} --")
        while True:
            label = prompt_required("  Short label (used for SSH alias & git aliases, e.g. personal/work/client1)",
                                     c.get("label", ""))
            if not LABEL_RE.match(label):
                print("  letters, digits, - and _ only, please.")
                continue
            if label.lower() in used_labels:
                print(f"  '{label}' is already used -- pick another.")
                continue
            break
        used_labels.add(label.lower())

        host = prompt("  Git host (github.com, gitlab.com, or a self-hosted domain)",
                       c.get("host", "github.com"))
        owners_cached = c.get("owners", [])
        owner = prompt_required(f"  Your username on {host} (the account you log in with)",
                                owners_cached[0] if owners_cached else "")
        extra = prompt("  Other orgs/owners on this host that belong to this account (comma-separated, optional)",
                        ", ".join(owners_cached[1:]))
        owners = [owner] + [o.strip() for o in extra.split(",") if o.strip()]
        name = prompt("  Git author name", c.get("name", owner))
        email = prompt_required("  Git email", c.get("email", ""))
        folder_in = prompt("  Dedicated folder for this account (optional -- blank = identify by remote only)",
                            c.get("folder", ""))
        folder = Path(folder_in) if folder_in else None
        generate = prompt_yn("  Generate a new SSH key for this account?", c.get("generate_key", True))
        key_path = Path(prompt("  SSH private key path",
                                c.get("key_path", str(home / ".ssh" / f"id_ed25519_{label}"))))

        accounts.append(Account(label, name, email, host, owners, key_path, generate, folder, False))
        i += 1
        print()
        if not prompt_yn("Add another account?", i < len(cached_accounts)):
            break
        print()

    print("\nAccounts configured:", ", ".join(a.label for a in accounts))
    cached_default = cache.get("accounts", [])
    cached_default_label = next((a["label"] for a in cached_default if a.get("is_default")), "")
    default_label = prompt(
        "Which account should be used for repos outside every dedicated folder with no matching "
        "remote yet? (blank = none)",
        cached_default_label if cached_default_label in used_labels else "",
    )
    for a in accounts:
        a.is_default = (a.label == default_label)

    setup_gh_answer = False
    if any(a.is_github for a in accounts):
        setup_gh_answer = prompt_yn(
            "Set up the GitHub CLI too (install gh if missing, log in each GitHub account, "
            "and make `gh` use the right account automatically)?",
            cache.get("setup_gh", True),
        )

    return SetupConfig(
        accounts=accounts,
        hooks_dir=Path(cache.get("hooks_dir", str(home / ".githooks"))),
        setup_gh=setup_gh_answer,
    )


# --------------------------------------------------------------------------
# GUI wizard
# --------------------------------------------------------------------------

def gui_wizard() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    home = Path.home()
    cache = load_cache()
    cached_accounts = cache.get("accounts", [])

    root = tk.Tk()
    root.title("Git multi-account setup")
    root.geometry("700x820")

    default_var = tk.StringVar(value="")  # holds the internal id of the default account, or ""
    account_rows: dict[str, dict] = {}     # internal_id -> {vars..., frame}
    next_id = [0]

    canvas = tk.Canvas(root, borderwidth=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas)
    body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    ttk.Label(
        body, wraplength=640, justify="left",
        text=("Add one entry per account you want to keep separate (GitHub, GitLab, or a "
              "self-hosted host). Give an account a dedicated folder if repos under it should "
              "always use it; mark at most one account 'default' for everything else."),
    ).pack(fill="x", padx=10, pady=(10, 0))

    if cache:
        ttk.Label(
            body, foreground="#2a6", wraplength=640, justify="left",
            text=f"Loaded your last settings (saved {cache.get('saved_at', 'earlier')}).",
        ).pack(fill="x", padx=10, pady=(4, 0))

    accounts_container = ttk.Frame(body)
    accounts_container.pack(fill="x", padx=0, pady=0)

    log_frame = ttk.LabelFrame(body, text="Log", padding=10)
    log_box = scrolledtext.ScrolledText(log_frame, height=14, state="disabled", wrap="word")
    log_box.pack(fill="both", expand=True)

    def log(msg: str):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    def add_account_row(cached: dict | None = None):
        cached = cached or {}
        internal_id = f"acct{next_id[0]}"
        next_id[0] += 1

        frame = ttk.LabelFrame(accounts_container, text=f"Account", padding=10)
        frame.pack(fill="x", padx=10, pady=6)

        vars_ = {}

        def row(label_text, key, default="", browse=None):
            r = ttk.Frame(frame)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label_text, width=26).pack(side="left")
            var = tk.StringVar(value=default)
            ttk.Entry(r, textvariable=var).pack(side="left", fill="x", expand=True)
            vars_[key] = var
            if browse == "dir":
                def do_browse():
                    d = filedialog.askdirectory(initialdir=var.get() or str(home))
                    if d:
                        var.set(d)
                ttk.Button(r, text="Browse", command=do_browse).pack(side="left", padx=4)
            elif browse == "file":
                def do_browse():
                    f = filedialog.askopenfilename(
                        initialdir=str(Path(var.get()).parent) if var.get() else str(home / ".ssh"))
                    if f:
                        var.set(f)
                ttk.Button(r, text="Browse", command=do_browse).pack(side="left", padx=4)
            return var

        owners_cached = cached.get("owners", [])
        row("Label (SSH alias, e.g. personal)", "label", cached.get("label", ""))
        row("Git host", "host", cached.get("host", "github.com"))
        row("Your username (login)", "owner", owners_cached[0] if owners_cached else "")
        row("Other orgs/owners (comma-sep)", "extra_owners", ", ".join(owners_cached[1:]))
        row("Git author name", "name", cached.get("name", ""))
        row("Git email", "email", cached.get("email", ""))
        row("Dedicated folder (optional)", "folder", cached.get("folder", ""), browse="dir")

        gen_var = tk.BooleanVar(value=cached.get("generate_key", True))
        ttk.Checkbutton(frame, text="Generate a new SSH key for this account", variable=gen_var).pack(anchor="w", pady=2)
        vars_["generate_key"] = gen_var

        row("SSH private key path", "key_path",
            cached.get("key_path", str(home / ".ssh" / "id_ed25519_new")), browse="file")

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Radiobutton(bottom, text="Default (used outside every folder)",
                         variable=default_var, value=internal_id).pack(side="left")

        def do_remove():
            if default_var.get() == internal_id:
                default_var.set("")
            frame.destroy()
            account_rows.pop(internal_id, None)

        ttk.Button(bottom, text="Remove account", command=do_remove).pack(side="right")

        account_rows[internal_id] = {"vars": vars_, "frame": frame}
        if cached.get("is_default"):
            default_var.set(internal_id)

    if cached_accounts:
        for c in cached_accounts:
            add_account_row(c)
    else:
        add_account_row()
        add_account_row()

    add_btn_frame = ttk.Frame(body)
    add_btn_frame.pack(fill="x", padx=10, pady=(0, 6))
    ttk.Button(add_btn_frame, text="+ Add another account", command=lambda: add_account_row()).pack(side="left")

    gh_frame = ttk.LabelFrame(body, text="GitHub CLI (gh)", padding=10)
    gh_frame.pack(fill="x", padx=10, pady=6)
    setup_gh_var = tk.BooleanVar(value=cache.get("setup_gh", True))
    ttk.Checkbutton(
        gh_frame, variable=setup_gh_var,
        text="Install gh if missing, log in each GitHub account, and make `gh` use the right account automatically",
    ).pack(anchor="w", pady=2)
    ttk.Label(
        gh_frame, wraplength=620, justify="left", foreground="#666",
        text="Each login opens your browser: sign it in to the account being asked for "
             "(or use a private window) before pasting the code.",
    ).pack(fill="x")

    adv_frame = ttk.LabelFrame(body, text="Advanced", padding=10)
    adv_frame.pack(fill="x", padx=10, pady=6)
    hooks_var = tk.StringVar(value=cache.get("hooks_dir", str(home / ".githooks")))
    hr = ttk.Frame(adv_frame)
    hr.pack(fill="x", pady=2)
    ttk.Label(hr, text="Hooks directory", width=26).pack(side="left")
    ttk.Entry(hr, textvariable=hooks_var).pack(side="left", fill="x", expand=True)

    def browse_hooks():
        d = filedialog.askdirectory(initialdir=hooks_var.get() or str(home))
        if d:
            hooks_var.set(d)
    ttk.Button(hr, text="Browse", command=browse_hooks).pack(side="left", padx=4)

    dry_run_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(adv_frame, text="Dry run (preview only, write nothing)", variable=dry_run_var).pack(anchor="w", pady=2)

    log_frame.pack(fill="both", expand=True, padx=10, pady=6)

    def collect_config() -> SetupConfig:
        accounts = []
        for internal_id, row_data in account_rows.items():
            v = row_data["vars"]
            owners = [v["owner"].get().strip()]
            owners += [o.strip() for o in v["extra_owners"].get().split(",") if o.strip()]
            folder_str = v["folder"].get().strip()
            accounts.append(Account(
                label=v["label"].get().strip(),
                name=v["name"].get().strip() or v["owner"].get().strip(),
                email=v["email"].get().strip(),
                host=v["host"].get().strip() or "github.com",
                owners=owners,
                key_path=Path(v["key_path"].get().strip()),
                generate_key=v["generate_key"].get(),
                folder=Path(folder_str) if folder_str else None,
                is_default=(default_var.get() == internal_id),
            ))
        return SetupConfig(accounts=accounts, hooks_dir=Path(hooks_var.get().strip()),
                           setup_gh=setup_gh_var.get())

    def gui_gh_login(gh: str, acct: Account) -> None:
        import queue
        import threading
        import webbrowser

        proc = subprocess.Popen(
            gh_login_command(gh, acct), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=gh_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
        reader.start()

        win = tk.Toplevel(root)
        win.title(f"gh login: {acct.gh_user} on {acct.host}")
        win.transient(root)
        message = tk.StringVar(value=f"Starting gh login for {acct.gh_user}...")
        ttk.Label(win, textvariable=message, wraplength=440, justify="left", padding=12).pack(fill="x")
        code_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=code_var, font=("TkFixedFont", 20, "bold"), padding=(12, 0)).pack()
        url = [f"https://{acct.host}/login/device"]
        buttons = ttk.Frame(win, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Open the page again", command=lambda: webbrowser.open(url[0])).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=proc.terminate).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", proc.terminate)

        def drain():
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    return
                if line.strip():
                    log("    gh: " + line.strip())
                found_url = re.search(r"(https://\S+/login/device)", line)
                if found_url:
                    url[0] = found_url.group(1)
                found_code = re.search(r"one-time code:\s*(\S+)", line)
                if found_code and not code_var.get():
                    code = found_code.group(1)
                    code_var.set(code)
                    root.clipboard_clear()
                    root.clipboard_append(code)
                    message.set(
                        f"1. In the browser page that just opened, make sure you're signed in to {acct.host} "
                        f"as {acct.gh_user} (or use a private window).\n"
                        "2. Paste this code (already copied) and authorize GitHub CLI.\n"
                        "This window closes by itself when gh is done."
                    )
                    root.after(300, lambda: webbrowser.open(url[0]))

        while proc.poll() is None:
            drain()
            root.update()
            time.sleep(0.05)
        reader.join(timeout=2)
        drain()
        win.destroy()

    def on_apply():
        cfg = collect_config()
        errors = validate_accounts(cfg.accounts)
        if errors:
            messagebox.showerror("Fix these first", "\n".join(errors))
            return

        dry_run = dry_run_var.get()
        apply_button.state(["disabled"])
        try:
            apply_config(cfg, dry_run, log, gh_login=gui_gh_login, pump=root.update, interactive=False)
            if not dry_run:
                messagebox.showinfo("Done", "Setup complete. See the log for details.")
        except Exception as e:  # surfaced to the user, not silently swallowed
            log(f"ERROR: {e}")
            messagebox.showerror("Setup failed", str(e))
        finally:
            apply_button.state(["!disabled"])

    btn_frame = ttk.Frame(body)
    btn_frame.pack(fill="x", padx=10, pady=10)
    apply_button = ttk.Button(btn_frame, text="Apply configuration", command=on_apply)
    apply_button.pack(side="right")

    root.mainloop()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def config_from_file(path: Path) -> SetupConfig:
    """Build a SetupConfig from a JSON file in the same format as the cache file
    (~/.git-account-setup-cache.json). Paths may use ~."""
    data = json.loads(path.read_text(encoding="utf-8"))
    accounts = []
    for c in data["accounts"]:
        label = c["label"]
        accounts.append(Account(
            label=label,
            name=c.get("name") or c["owners"][0],
            email=c["email"],
            host=c.get("host", "github.com"),
            owners=list(c["owners"]),
            key_path=Path(c.get("key_path") or f"~/.ssh/id_ed25519_{label}"),
            generate_key=c.get("generate_key", True),
            folder=Path(c["folder"]) if c.get("folder") else None,
            is_default=c.get("is_default", False),
        ))
    return SetupConfig(
        accounts=accounts,
        hooks_dir=Path(data.get("hooks_dir", "~/.githooks")).expanduser(),
        setup_gh=data.get("setup_gh", False),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cli", action="store_true", help="use the terminal wizard instead of the GUI")
    parser.add_argument("--dry-run", action="store_true", help="(CLI mode) preview changes, write nothing")
    parser.add_argument("--config", metavar="FILE",
                        help="apply a JSON config (same format as ~/.git-account-setup-cache.json) "
                             "without asking questions -- copy it to another laptop for a one-command setup")
    parser.add_argument("--from-cache", action="store_true",
                        help="like --config, using ~/.git-account-setup-cache.json")
    args = parser.parse_args()

    if args.config or args.from_cache:
        cfg_path = Path(args.config).expanduser() if args.config else CACHE_FILE
        cfg = config_from_file(cfg_path)
        apply_config(cfg, args.dry_run, print, interactive=False)
        return

    if args.cli:
        cfg = cli_wizard()
        apply_config(cfg, args.dry_run, print)
        return

    try:
        gui_wizard()
    except ImportError:
        print("tkinter is not available on this system -- falling back to the terminal wizard.\n")
        cfg = cli_wizard()
        apply_config(cfg, args.dry_run, print)


if __name__ == "__main__":
    main()
