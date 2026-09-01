#!/usr/bin/env python3
"""
git_account_setup.py -- separate two GitHub accounts (e.g. personal + work)
on one machine.

What it sets up, per account:
  - an SSH host alias (~/.ssh/config) pointing at github.com with a
    dedicated key, IdentitiesOnly yes
  - a small identity file (~/.gitconfig-<label>) with [user] name/email
  - owner-scoped URL rewriting in ~/.gitconfig, so ANY
    https://github.com/<owner>/... remote -- typed by hand, `git clone`,
    a GUI client, whatever -- is transparently rewritten to the right
    SSH alias. This is scoped per GitHub owner (not a single blanket
    rule), so it works correctly no matter which folder you're in or how
    the remote was added -- there is no "new repo created outside the
    wrapper" gap.
  - includeIf rules so git picks the right [user] identity from either
    the folder a repo lives in (one designated folder = the "personal"
    account; everywhere else defaults to "work") or the remote URL/host
    alias it points at, whichever is known first.
  - pre-commit / pre-push hooks (via core.hooksPath) that refuse to
    commit or push under the wrong identity for a given remote, so a
    misconfigured repo fails loudly and locally instead of leaking the
    wrong account upstream.
  - `git whoami`, `git use-personal`, `git use-work`, `git clone-personal`,
    `git clone-work` aliases.

Every write is preceded by a timestamped backup of the files it touches,
and re-running the script is idempotent (it replaces its own previously
written block instead of duplicating it).

Usage:
    python setup_git_accounts.py            # GUI wizard (tkinter)
    python setup_git_accounts.py --cli      # terminal wizard, no tkinter needed
    python setup_git_accounts.py --cli --dry-run   # show what would happen, write nothing

Works on Windows, macOS and Linux (Python 3.8+, stdlib only; tkinter is
optional and only needed for the GUI wizard).
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

BEGIN_MARK = "# >>> git-multi-account (managed by git_account_setup.py) >>>"
END_MARK = "# <<< git-multi-account (managed by git_account_setup.py) <<<"

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


def to_posix(p: Path) -> str:
    """git config / ssh config always want forward slashes, even on Windows."""
    return str(p).replace("\\", "/")


@dataclass
class Account:
    label: str            # short id used as SSH host alias & hook/alias names, e.g. "personal", "work"
    name: str              # git author name
    email: str
    owners: list[str]      # GitHub usernames/orgs that belong to this account
    key_path: Path
    generate_key: bool = False

    @property
    def identity_file(self) -> Path:
        return Path.home() / f".gitconfig-{self.label}"


@dataclass
class SetupConfig:
    personal: Account
    work: Account
    personal_folder: Path
    hooks_dir: Path = field(default_factory=lambda: Path.home() / ".githooks")
    home: Path = field(default_factory=Path.home)


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
        shutil.copy2(p, backup_dir / p.name)
    log(f"backed up {len(existing)} existing file(s) to {backup_dir}")
    return backup_dir


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
    for acct in (cfg.personal, cfg.work):
        if f"Host {acct.label}\n" in existing or existing.strip() == f"Host {acct.label}":
            log(f"  ~/.ssh/config already has 'Host {acct.label}' -- leaving it untouched")
            continue
        new_blocks.append(
            f"Host {acct.label}\n"
            f"    HostName github.com\n"
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
        f"# {acct.label} GitHub identity -- managed by git_account_setup.py\n"
        f"[user]\n"
        f"\tname = {acct.name}\n"
        f"\temail = {acct.email}\n"
        f"\n"
        f'[credential "https://github.com"]\n'
        f"\tusername = {acct.owners[0]}\n"
    )
    log(f"  writing {acct.identity_file}")
    if not dry_run:
        acct.identity_file.write_text(content, newline="\n")


def build_managed_block(cfg: SetupConfig) -> str:
    lines = [BEGIN_MARK]
    lines.append(f"# personal: {', '.join(cfg.personal.owners)} <{cfg.personal.email}>")
    lines.append(f"# work:     {', '.join(cfg.work.owners)} <{cfg.work.email}>")

    # Default identity. Git config is "last value in file order wins", and an
    # includeIf's content is spliced in at the point the includeIf directive
    # appears -- so this [user] block MUST come before the includeIf rules
    # below, or the default would always win over any conditional match.
    lines.append("[user]")
    lines.append(f"\tname = {cfg.work.name}")
    lines.append(f"\temail = {cfg.work.email}")

    # Owner-scoped URL rewriting: any https://github.com/<owner>/... remote
    # is rewritten to the right SSH alias, regardless of current folder or
    # how the remote was added (init+remote add, clone, GUI client, ...).
    for acct in (cfg.personal, cfg.work):
        for owner in acct.owners:
            lines.append(f'[url "git@{acct.label}:"]')
            lines.append(f"\tinsteadOf = https://github.com/{owner}/")

    # Folder rule: this designated folder always uses the personal identity,
    # even before a remote exists (e.g. `git init` + first commit).
    folder = to_posix(cfg.personal_folder)
    if not folder.endswith("/"):
        folder += "/"
    lines.append(f'[includeIf "gitdir/i:{folder}"]')
    lines.append(f"\tpath = {to_posix(cfg.personal.identity_file)}")

    # Remote-based rules: whichever identity matches the actual remote wins,
    # so a personal repo living outside the personal folder (or vice versa)
    # still gets the right author once a remote is attached.
    for acct in (cfg.personal, cfg.work):
        for pattern in (
            f"git@{acct.label}:**/**",
            f"ssh://git@{acct.label}/**",
        ):
            lines.append(f'[includeIf "hasconfig:remote.*.url:{pattern}"]')
            lines.append(f"\tpath = {to_posix(acct.identity_file)}")
        for owner in acct.owners:
            lines.append(f'[includeIf "hasconfig:remote.*.url:https://github.com/{owner}/**"]')
            lines.append(f"\tpath = {to_posix(acct.identity_file)}")

    lines.append(END_MARK)
    return "\n".join(lines) + "\n"


def strip_managed_block(content: str) -> str:
    if BEGIN_MARK not in content:
        return content
    pre, _, rest = content.partition(BEGIN_MARK)
    _, _, post = rest.partition(END_MARK)
    return pre.rstrip("\n") + ("\n" if pre.strip() else "") + post.lstrip("\n")


def whoami_alias(cfg: SetupConfig) -> str:
    cases = "".join(
        f"{acct.email}) echo '{acct.label.upper()} ({acct.owners[0]})';; "
        for acct in (cfg.personal, cfg.work)
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
        f'&& echo "this repo -> {acct.label.upper()} ({acct.owners[0]})"; }}; f'
    )


def clone_alias(label: str) -> str:
    return (
        "!f() { r=$(printf '%s' \"$1\" | sed -e 's|^https://github.com/||' "
        "-e 's|^git@[^:]*:||' -e 's|^ssh://git@[^/]*/||'); r=${r%.git}; shift; "
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
    git_config(["alias.use-personal", use_alias(cfg.personal)])
    git_config(["alias.use-work", use_alias(cfg.work)])
    git_config(["alias.clone-personal", clone_alias(cfg.personal.label)])
    git_config(["alias.clone-work", clone_alias(cfg.work.label)])


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

def build_pre_commit(cfg: SetupConfig) -> str:
    cases = "\n".join(
        f'    {acct.label}:*|ssh://git@{acct.label}/*|{acct.label}:*)\n'
        f'        expected="{acct.email}"; label=\'{acct.label.upper()} '
        f"(github.com/{acct.owners[0]})'; fix='git use-{acct.label}' ;;"
        .replace(f"{acct.label}:*|", f"git@{acct.label}:*|")
        for acct in (cfg.personal, cfg.work)
    )
    return f"""\
#!/bin/sh
# Identity guard, commit-time. Blocks a commit whose author address does not
# match the GitHub account this repo's origin belongs to.
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
    other = {cfg.personal.label: cfg.work, cfg.work.label: cfg.personal}
    cases = []
    for acct in (cfg.personal, cfg.work):
        forbidden = other[acct.label].email
        cases.append(
            f'    git@{acct.label}:*|ssh://git@{acct.label}/*|{acct.label}:*)\n'
            f'        account="{acct.label.upper()} (github.com/{acct.owners[0]})"; '
            f'expected="{acct.email}"; forbidden="{forbidden}" ;;'
        )
    cases_str = "\n".join(cases)
    return f"""\
#!/bin/sh
# Identity guard, push-time. Refuses to send commits authored with the wrong
# account's address for this remote.
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
    *github.com*)
        cat >&2 <<MSG

  PUSH BLOCKED -- ambiguous GitHub remote

    remote: $remote_url

  This URL does not say which of your accounts it belongs to. Point it at a
  host alias instead, e.g.:

    git remote set-url $remote_name git@{cfg.personal.label}:<owner>/<repo>.git
    git remote set-url $remote_name git@{cfg.work.label}:<owner>/<repo>.git

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

  Fix with:  git use-{cfg.personal.label}   or   git use-{cfg.work.label}

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
    git log "$@" --author="$forbidden" --format='%h  %an <%ae>  %s' 2>/dev/null >> "$offenders"
done

if [ -s "$offenders" ]; then
    cat >&2 <<MSG

  PUSH BLOCKED -- commits authored with the other account

    remote belongs to : $account
    but these commits were authored as $forbidden

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
    folder_lower = str(cfg.personal_folder).replace("\\", "/").lower()
    return f"""\
#!/bin/sh
# A repo cloned into the personal folder should talk to GitHub as the
# personal account, so retarget a bare github.com origin at the personal
# SSH alias (belt-and-suspenders: the owner-scoped insteadOf rule in
# ~/.gitconfig should already have rewritten it before clone).
top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
lower=$(printf '%s' "$top" | tr 'A-Z' 'a-z')
case "$lower" in
    {folder_lower}|{folder_lower}/*)
        url=$(git remote get-url origin 2>/dev/null)
        case "$url" in
            https://github.com/*|git@github.com:*|ssh://git@github.com/*)
                repo=$(printf '%s' "$url" | sed -e 's|^https://github.com/||' \\
                                                -e 's|^git@github.com:||' \\
                                                -e 's|^ssh://git@github.com/||')
                git remote set-url origin "git@{cfg.personal.label}:$repo"
                echo "  note: origin retargeted to the personal account -> git@{cfg.personal.label}:$repo" >&2 ;;
        esac ;;
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
# orchestration
# --------------------------------------------------------------------------

def apply_config(cfg: SetupConfig, dry_run: bool, log) -> None:
    log("== backing up existing files ==")
    to_backup = [
        cfg.home / ".gitconfig",
        cfg.home / ".ssh" / "config",
        cfg.personal.identity_file,
        cfg.work.identity_file,
    ]
    if not dry_run:
        backup_existing(cfg.home, to_backup, log)
    else:
        log("  (skipped: --dry-run)")

    log("== SSH keys ==")
    ensure_ssh_key(cfg.personal, dry_run, log)
    ensure_ssh_key(cfg.work, dry_run, log)

    log("== SSH config ==")
    write_ssh_config(cfg, dry_run, log)

    log("== identity files ==")
    write_identity_file(cfg.personal, dry_run, log)
    write_identity_file(cfg.work, dry_run, log)

    log("== global .gitconfig ==")
    update_global_gitconfig(cfg, dry_run, log)

    log("== hooks ==")
    write_hooks(cfg, dry_run, log)

    log("")
    log("Done." if not dry_run else "Dry run complete -- nothing was written.")
    log("")
    log("Verify with, from any repo:")
    log("  git whoami")
    log(f"  ssh -T git@{cfg.personal.label}")
    log(f"  ssh -T git@{cfg.work.label}")


# --------------------------------------------------------------------------
# CLI wizard
# --------------------------------------------------------------------------

def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{text}{suffix}: ").strip()
    return val or default


def prompt_required(text: str) -> str:
    while True:
        val = input(f"{text}: ").strip()
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
    default_personal_folder = str(home / "Projects" / "Personal")
    personal_folder = Path(prompt(
        "Folder that should ALWAYS use the personal account (everything else defaults to work)",
        default_personal_folder,
    ))

    print("\n-- Personal account --")
    p_owner = prompt_required("  GitHub username")
    p_name = prompt("  Git author name", p_owner)
    p_email = prompt_required("  Git email")
    p_extra_owners = prompt("  Any other GitHub owners/orgs for this account (comma-separated, optional)", "")
    p_owners = [p_owner] + [o.strip() for o in p_extra_owners.split(",") if o.strip()]
    p_generate = prompt_yn("  Generate a new SSH key for this account?", True)
    p_key = Path(prompt("  SSH private key path", str(home / ".ssh" / "id_ed25519_personal")))

    print("\n-- Work account --")
    w_owner = prompt_required("  GitHub username or org")
    w_name = prompt("  Git author name", w_owner)
    w_email = prompt_required("  Git email")
    w_extra_owners = prompt("  Any other GitHub owners/orgs for this account (comma-separated, optional)", "")
    w_owners = [w_owner] + [o.strip() for o in w_extra_owners.split(",") if o.strip()]
    w_generate = prompt_yn("  Generate a new SSH key for this account?", True)
    w_key = Path(prompt("  SSH private key path", str(home / ".ssh" / "id_ed25519_work")))

    personal = Account("personal", p_name, p_email, p_owners, p_key, p_generate)
    work = Account("work", w_name, w_email, w_owners, w_key, w_generate)
    return SetupConfig(personal=personal, work=work, personal_folder=personal_folder)


# --------------------------------------------------------------------------
# GUI wizard
# --------------------------------------------------------------------------

def gui_wizard() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    home = Path.home()
    root = tk.Tk()
    root.title("Git multi-account setup")
    root.geometry("640x720")

    vars_ = {}

    def add_section(parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        frame.pack(fill="x", padx=10, pady=6)
        return frame

    def add_row(frame, key, label, default="", browse=None):
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=26).pack(side="left")
        var = tk.StringVar(value=default)
        entry = ttk.Entry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True)
        vars_[key] = var
        if browse == "dir":
            def do_browse():
                d = filedialog.askdirectory(initialdir=var.get() or str(home))
                if d:
                    var.set(d)
            ttk.Button(row, text="Browse", command=do_browse).pack(side="left", padx=4)
        elif browse == "file":
            def do_browse():
                f = filedialog.askopenfilename(initialdir=str(Path(var.get()).parent) if var.get() else str(home / ".ssh"))
                if f:
                    var.set(f)
            ttk.Button(row, text="Browse", command=do_browse).pack(side="left", padx=4)
        return var

    def add_check(frame, key, label, default=True):
        var = tk.BooleanVar(value=default)
        ttk.Checkbutton(frame, text=label, variable=var).pack(anchor="w", pady=2)
        vars_[key] = var
        return var

    canvas = tk.Canvas(root, borderwidth=0)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas)
    body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    intro = ttk.Label(
        body,
        wraplength=580,
        justify="left",
        text=(
            "This designates one folder that always uses your PERSONAL GitHub "
            "account. Every other repo defaults to WORK, unless its remote "
            "points at a personal owner/org, in which case that repo is "
            "personal too regardless of folder."
        ),
    )
    intro.pack(fill="x", padx=10, pady=(10, 0))

    folder_frame = add_section(body, "Personal folder")
    add_row(folder_frame, "personal_folder", "Folder", str(home / "Projects" / "Personal"), browse="dir")

    p_frame = add_section(body, "Personal account")
    add_row(p_frame, "p_name", "Git author name", "")
    add_row(p_frame, "p_email", "Git email", "")
    add_row(p_frame, "p_owner", "GitHub username", "")
    add_row(p_frame, "p_extra_owners", "Other owners/orgs (comma-sep, optional)", "")
    add_check(p_frame, "p_generate", "Generate a new SSH key for this account", True)
    add_row(p_frame, "p_key", "SSH private key path", str(home / ".ssh" / "id_ed25519_personal"), browse="file")

    w_frame = add_section(body, "Work account")
    add_row(w_frame, "w_name", "Git author name", "")
    add_row(w_frame, "w_email", "Git email", "")
    add_row(w_frame, "w_owner", "GitHub username or org", "")
    add_row(w_frame, "w_extra_owners", "Other owners/orgs (comma-sep, optional)", "")
    add_check(w_frame, "w_generate", "Generate a new SSH key for this account", True)
    add_row(w_frame, "w_key", "SSH private key path", str(home / ".ssh" / "id_ed25519_work"), browse="file")

    adv_frame = add_section(body, "Advanced")
    add_row(adv_frame, "hooks_dir", "Hooks directory", str(home / ".githooks"), browse="dir")
    add_check(adv_frame, "dry_run", "Dry run (preview only, write nothing)", False)

    log_frame = add_section(body, "Log")
    log_box = scrolledtext.ScrolledText(log_frame, height=14, state="disabled", wrap="word")
    log_box.pack(fill="both", expand=True)

    def log(msg: str):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    def collect_config() -> SetupConfig:
        p_owners = [vars_["p_owner"].get().strip()]
        p_owners += [o.strip() for o in vars_["p_extra_owners"].get().split(",") if o.strip()]
        w_owners = [vars_["w_owner"].get().strip()]
        w_owners += [o.strip() for o in vars_["w_extra_owners"].get().split(",") if o.strip()]

        personal = Account(
            "personal", vars_["p_name"].get().strip(), vars_["p_email"].get().strip(),
            p_owners, Path(vars_["p_key"].get().strip()), vars_["p_generate"].get(),
        )
        work = Account(
            "work", vars_["w_name"].get().strip(), vars_["w_email"].get().strip(),
            w_owners, Path(vars_["w_key"].get().strip()), vars_["w_generate"].get(),
        )
        return SetupConfig(
            personal=personal,
            work=work,
            personal_folder=Path(vars_["personal_folder"].get().strip()),
            hooks_dir=Path(vars_["hooks_dir"].get().strip()),
        )

    def on_apply():
        cfg = collect_config()
        missing = []
        for label, val in [
            ("Personal folder", str(cfg.personal_folder)),
            ("Personal email", cfg.personal.email), ("Personal GitHub username", cfg.personal.owners[0]),
            ("Work email", cfg.work.email), ("Work GitHub username/org", cfg.work.owners[0]),
        ]:
            if not val:
                missing.append(label)
        if missing:
            messagebox.showerror("Missing fields", "Please fill in:\n" + "\n".join(missing))
            return
        if not cfg.personal.name:
            cfg.personal.name = cfg.personal.owners[0]
        if not cfg.work.name:
            cfg.work.name = cfg.work.owners[0]

        dry_run = vars_["dry_run"].get()
        try:
            apply_config(cfg, dry_run, log)
            if not dry_run:
                messagebox.showinfo("Done", "Setup complete. See the log for details.")
        except Exception as e:  # surfaced to the user, not silently swallowed
            log(f"ERROR: {e}")
            messagebox.showerror("Setup failed", str(e))

    btn_frame = ttk.Frame(body)
    btn_frame.pack(fill="x", padx=10, pady=10)
    ttk.Button(btn_frame, text="Apply configuration", command=on_apply).pack(side="right")

    root.mainloop()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cli", action="store_true", help="use the terminal wizard instead of the GUI")
    parser.add_argument("--dry-run", action="store_true", help="(CLI mode) preview changes, write nothing")
    args = parser.parse_args()

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
