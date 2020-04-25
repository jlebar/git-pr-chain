#!/usr/bin/env python3

# Requires
#  pip3 install pyyaml
#  pip3 install PyGithub

import argparse
import collections
import concurrent.futures
import datetime
import functools
import inspect
import itertools
import json
import json
import os
import re
import subprocess
import sys
import textwrap
import yaml
from typing import List, Dict, Tuple, Iterable, Optional

# Not compatible with pytype; ignore using instructions from
# https://github.com/google/pytype/issues/80
import github  # type: ignore

VERBOSE = False
DRY_RUN = False


def fatal(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def traced(fn, show_start=False, show_end=True):
    """Decorator that "traces" a function under --verbose.

    - If VERBOSE and show_start are true, prints a message when the function
      starts.
    - If VERBOSE show_end are true, prints a message when the function
      ends.
    """

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        starttime = datetime.datetime.now()
        if VERBOSE and show_start:
            print(f"{fn.__name__}({args}, {kwargs}) starting")
        ret = fn(*args, **kwargs)
        if VERBOSE and show_end:
            one_ms = datetime.timedelta(milliseconds=1)
            ms = (datetime.datetime.now() - starttime) / one_ms
            print(f"{fn.__name__}({args}, {kwargs}) returned in {ms:.0f}ms: {ret}")
        return ret

    return inner


@functools.lru_cache()
def gh_client():
    # Try to get the github oauth token out of gh or hub's config file.  Yes, I
    # am that evil.
    config_dir = os.environ.get(
        "XDG_CONFIG_HOME", os.path.join(os.environ["HOME"], ".config")
    )

    def get_token_from(fname):
        try:
            with open(os.path.join(config_dir, fname)) as f:
                config = yaml.safe_load(f)
                return config["github.com"][0]["oauth_token"]
        except (FileNotFoundError, KeyError, IndexError):
            return None

    token = None
    for fname in ["gh/config.yml", "hub"]:
        token = get_token_from(fname)
        if token:
            break
    if not token:
        fatal(
            "Couldn't get oauth token from gh or hub.  Install one of those "
            "tools and authenticate to github."
        )
    return github.Github(token)


@functools.lru_cache()
@traced
def gh_repo_client():
    remote = git_upstream_remote()

    # Translate our remote's URL into a github user/repo string.  (Is there
    # seriously not a beter way to do this?)
    remote_url = git("remote", "get-url", remote)
    match = re.search(r"(?:[/:])([^/:]+/[^/:]+)\.git$", remote_url)
    if not match:
        fatal(
            f"Couldn't extract github user/repo from {remote} "
            f"remote URL {remote_url}."
        )
    gh_repo_name = match.group(1)
    return gh_client().get_repo(gh_repo_name)


class cached_property:
    """
    (Bad) backport of python3.8's functools.cached_property.
    """

    def __init__(self, fn):
        self.__doc__ = fn.__doc__
        self.fn = fn

    def __get__(self, instance, cls):
        if instance is None:
            return self
        val = self.fn(instance)
        instance.__dict__[self.fn.__name__] = val
        return val


@traced
def git(*args, err_ok=False, stderr_to_stdout=False):
    """Runs a git command, returning the output."""
    stderr = subprocess.DEVNULL if err_ok else None
    stderr = subprocess.STDOUT if stderr_to_stdout else stderr
    try:
        return (
            subprocess.check_output(["git"] + list(args), stderr=stderr)
            .decode("utf-8")
            .strip()
        )
    except subprocess.CalledProcessError:
        if not err_ok:
            raise
        return ""


class Commit:
    # TODO: We could compute some/all of these properties with a single call to
    # git, rather than one for each.  Indeed, we could do it with a single call
    # to git for *all* of the commits we're interested in, all at once.  Does
    # it matter?  Maybe not, network ops are so much slower.

    def __init__(self, sha: str, parent: Optional["Commit"]):
        self.sha = sha
        self.parent = parent

    @cached_property
    @traced
    def gh_branch(self):
        """Branch that contains this commit in github."""
        if self.not_to_be_pushed:
            return None

        # Search the commit message for 'git-pr-chain: XYZ' or 'GPC: XYZ'.
        matches = re.findall(
            r"^(?:git-pr-chain|GPC):\s*(.*)", self.commit_msg, re.MULTILINE
        )
        if not matches:
            return self.parent.gh_branch if self.parent else None
        if len(matches) == 1:
            # findall returns the groups directly, so matches[0] is group 1 of
            # the match -- which is what we want.
            return matches[0]
        fatal(
            f"Commit {self.sha} has multiple git-pr-chain lines.  Rewrite "
            "history and fix this."
        )

    @cached_property
    @traced
    def not_to_be_pushed(self) -> bool:
        if self.parent and self.parent.not_to_be_pushed:
            return True

        # Search the commit message for 'git-pr-chain: STOP' or 'GPC: STOP'.
        return bool(re.search(r"(git-pr-chain|GPC):\s*STOP\>", self.commit_msg))

    @cached_property
    @traced
    def is_merge_commit(self):
        return len(git("show", "--no-patch", "--format=%P", self.sha).split("\n")) > 1

    @cached_property
    @traced
    def shortdesc(self):
        shortsha = git("rev-parse", "--short", self.sha)
        shortmsg = git("show", "--no-patch", "--format=%s", self.sha)
        return f"{shortsha} {shortmsg}"

    @cached_property
    @traced
    def commit_msg(self):
        return git("show", "--no-patch", "--format=%B", self.sha)


@functools.lru_cache()
@traced
def git_upstream_branch(branch=None):
    """Gets the upstream branch tracked by `branch`.

    If `branch` is none, uses the current branch.
    """
    if not branch:
        branch = "HEAD"
    branchref = git("rev-parse", "--symbolic-full-name", branch)
    return git("for-each-ref", "--format=%(upstream:short)", branchref)


def git_upstream_remote(branch=None):
    # Get the name of the upstream remote (e.g. "origin") that this branch is
    # downstream from.
    return git_upstream_branch(branch).split("/")[0]


@functools.lru_cache()
@traced
def gh_branch_prefix():
    return git("config", "pr-chain.branch-prefix").strip()


@functools.lru_cache()
def grouped_commits() -> List[Tuple[Optional[str], List[Commit]]]:
    return [
        (gh_branch_prefix() + branch if branch else None, list(cs))
        for branch, cs in itertools.groupby(branch_commits(), lambda c: c.gh_branch)
    ]


@functools.lru_cache()
def grouped_commits_to_push() -> List[Tuple[str, List[Commit]]]:
    return [(branch, cs) for branch, cs in grouped_commits() if branch]


def validate_branch_commits(commits: Iterable[Commit]) -> None:
    def list_strs(strs):
        return "\n".join("  - " + s for s in strs)

    def list_commits(cs):
        return list_strs(c.shortdesc for c in cs)

    merge_commits = [c for c in commits if c.is_merge_commit]
    if merge_commits:
        fatal(
            textwrap.dedent(
                f"""\
                History contained merge commit(s):

                {list_commits(merge_commits)}

                Merges are incompatible with git-pr-chain.  Rewrite your branch
                to a linear history."""
            )
        )

    # Count the number of times each github branch appears in grouped_commits.
    # If a branch appears more than once, that means we have an "AABA"
    # situation, where we go to one branch, then to another, then back to the
    # first.  That's not allowed.
    #
    # Ignore the `None` branch here.  It's allowed to appear at the beginning
    # and end of the list (but nowhere else!), and that invariant is checked
    # below.
    grouped_commits = [
        (branch, list(cs))
        for branch, cs in itertools.groupby(commits, lambda c: c.gh_branch)
    ]
    ctr = collections.Counter(branch for branch, _ in grouped_commits)
    repeated_branches = [branch for branch, count in ctr.items() if count > 1]
    if repeated_branches:
        fatal(
            textwrap.dedent(
                f"""\
                Upstream branch(es) AABA problem.  The following upstream
                branches appear, then are interrupted by a different branch,
                then reappear.

                {list_strs(repeated_branches)}

                This is not allowed; reorder commits or change their upstream
                branches."""
            )
        )

    # Check for github_branch == None; this is disallowed except for the first
    # and last group.  I don't think users can get into this situation
    # themselves.
    commits_without_branch = list(
        itertools.chain.from_iterable(
            cs for branch, cs in list(grouped_commits)[1:-1] if not branch
        )
    )
    if commits_without_branch:
        fatal(
            textwrap.dedent(
                f"""\
              Unable to infer upstream branches for commit(s):

              {list_commits(commits_without_branch)}

              This shouldn't happen and is probably a bug in git-pr-chain."""
            )
        )


@traced
def branch_commits() -> List[Commit]:
    """Get the commits in this branch.

    The first commit is the one connected to the branch base.  The last commit
    is HEAD.
    """
    upstream_branch = git_upstream_branch()
    if not upstream_branch:
        fatal(
            "Set an upstream branch with e.g. `git branch "
            "--set-upstream-to origin/master`"
        )
    commits = []
    for sha in (
        git("log", "--reverse", "--pretty=%H", f"{upstream_branch}..HEAD")
        .strip()
        .split("\n")
    ):
        # Filter out empty commits (if e.g. the git log produces nothing).
        if not sha:
            continue
        parent = commits[-1] if commits else None
        commits.append(Commit(sha, parent=parent))

    # Infer upstream branch names on commits that don't have one explicitly in
    # the commit message
    for idx, c in enumerate(commits):
        if idx == 0:
            continue
        if not c.gh_branch:
            c.inferred_upstream_branch = commits[idx - 1].gh_branch

    validate_branch_commits(commits)
    return commits


def cmd_show(args):
    commits = branch_commits()
    if not commits:
        fatal(
            "No commits in branch.  Is the upstream branch (git branch "
            "--set-upstream-to <origin/master or something> set correctly?"
        )

    print(
        f"Current branch is downstream from {git_upstream_branch()}, "
        f"{len(commits)} commit(s) ahead.\n"
    )
    for branch, cs in itertools.groupby(commits, lambda c: c.gh_branch):
        cs = list(cs)

        # TODO Link to PR if it exists.
        # TODO Link to branch on github
        if branch:
            print(f"Github branch {branch}")
        else:
            first = cs[0]
            if first.not_to_be_pushed:
                print("Will not be pushed; remove git-pr-chain:STOP to push.")
            else:
                print(
                    f"No github branch; will not be pushed. "
                    '(Add "git-pr-chain: <branch>" to commit msg.)'
                )
        for c in cs:
            # Indent two spaces, then call git log directly, so we get nicely
            # colorized output.
            print("  ", end="")
            sys.stdout.flush()
            subprocess.run(("git", "--no-pager", "log", "-n1", "--oneline", c.sha))


def push_branches(args):
    def push(branch_and_commits: Tuple[str, List[Commit]]):
        branch, cs = branch_and_commits
        remote = git_upstream_remote()
        msg = f"Pushing {branch} to {remote}"
        if not DRY_RUN:
            msg += ":\n"
            msg += git(
                "push",
                "-f",
                remote,
                f"{cs[-1].sha}:refs/heads/{branch}",
                stderr_to_stdout=True,
            )
            msg += "\n"

        # Print the message with just one print statement so that there's no
        # interleaving of the multiple concurrent pushes.
        print(msg)

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        executor.map(push, grouped_commits())


def chain_desc_for(
    gh_branch: str,
    cs: List[Commit],
    open_prs: Dict[str, List[github.PullRequest.PullRequest]],
) -> str:
    chain = []
    for branch, _ in grouped_commits_to_push():
        pr = open_prs[branch][0]  # should only be one at this point.
        line = f"#{pr.number} {pr.title}"
        if pr.head.ref == gh_branch:
            line = f"👉 {line} 👈 **YOU ARE HERE**"
        chain.append(f"1. {line}")
    chain_str = "\n".join(chain)

    commits_str = "\n".join(
        "1. "
        + (
            re.sub(r"^\s*(git-pr-chain|GPC):.*", "", c.commit_msg, flags=re.MULTILINE)
            .replace("\n", "\n    ")
            .strip()
        )
        for c in cs
    )

    return f"""\
<git-pr-chain>

#### Commits in this PR
{commits_str}

#### [PR chain](https://github.com/jlebar/git-pr-chain)
{chain_str}

</git-pr-chain>
"""


def create_and_update_prs(args):
    commits = branch_commits()
    repo = gh_repo_client()

    # Nominally you can ask the GH API for all PRs whose head is a particular
    # branch.  But this filter doesn't seem to do anything.  So instead, we
    # just pull all open PRs and filter them ourselves.
    #
    # TODO: If this is slow, we can kick it off asynchronously, before
    # push_branches.
    open_prs = collections.defaultdict(list)
    for pr in repo.get_pulls(state="open"):
        open_prs[pr.head.ref].append(pr)

    # Check that every branch has zero or one open PRs.  Otherwise, the
    # situation is ambiguous and we bail.
    for branch, _ in grouped_commits_to_push():
        branch_prs = open_prs[branch]
        if len(branch_prs) <= 1:
            continue
        joined_pr_urls = "\n".join("  - " + pr.html_url for pr in branch_prs)
        fatal(
            textwrap.dedent(
                f"""\
                Branch {branch} has multiple open PRs:
                {joined_pr_urls}
                Don't know which to choose!"""
            )
        )

    def base_for(idx) -> str:
        if idx == 0:
            return git_upstream_branch().split("/")[-1]
        base, _ = grouped_commits_to_push()[idx - 1]
        return base

    # Create new PRs if necessary.
    for idx, (branch, cs) in enumerate(grouped_commits_to_push()):
        branch_prs = open_prs[branch]
        if branch_prs:
            continue

        base = base_for(idx)
        print(f"Creating PR for {branch}, base {base}...")
        # TODO: Open an editor for title and body?  (Do both at once.)
        if not DRY_RUN:
            branch_prs.append(
                repo.create_pull(title=branch, base=base, head=branch, body="")
            )
            # TODO: Auto-open this URL.
            print(f"Created {pr.html_url}")

    # Update PRs for each branch.
    for idx, (branch, cs) in enumerate(grouped_commits_to_push()):
        branch_prs = open_prs[branch]
        if len(branch_prs) != 1:
            fatal(
                f"Expected one open PR for {branch}, but was {len(branch_prs)}."
                "This should not happen here!"
            )

        pr = branch_prs[0]

        base = base_for(idx)
        if pr.base.ref != base:
            print(
                f"Updating base branch for {branch} (PR #{pr.number}) "
                f"from {pr.base.ref} to {base}"
            )

        # Update <git-pr-chain> portion of description if necessary.
        body = pr.body
        if "<git-pr-chain>" not in body:
            body = body + "\n\n" + "<git-pr-chain></git-pr-chain>"

        body = re.sub(
            r"\s*<git-pr-chain>.*</git-pr-chain>",
            chain_desc_for(branch, cs, open_prs),
            body,
            flags=re.DOTALL,
        )

        if not DRY_RUN and (base != pr.base.ref or body != pr.body):
            pr.edit(base=base, body=body)


def cmd_upload(args):
    push_branches(args)
    create_and_update_prs(args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true")
    subparser = parser.add_subparsers()

    sp_show = subparser.add_parser("show", help="List commits in chain")
    sp_show.set_defaults(func=cmd_show)

    sp_upload = subparser.add_parser("upload", help="Create and update PRs in github")
    sp_upload.set_defaults(func=cmd_upload)

    def cmd_help(args):
        if "command" not in args or not args.command:
            parser.print_help()
        elif args.command == "show":
            sp_show.print_help()
        elif args.command == "upload":
            sp_upload.print_help()
        elif args.command == "help":
            print("Well aren't you trying to be clever.", file=sys.stderr)
        else:
            print(f"Unrecognized subcommand {args.command}", file=sys.stderr)
            parser.print_help()
            sys.exit(1)

    sp_help = subparser.add_parser("help")
    sp_help.add_argument("command", nargs="?", help="Subcommand to get help on")
    sp_help.set_defaults(func=cmd_help)

    args = parser.parse_args()
    if "func" not in args:
        print("Specify a subcommand!")
        parser.print_help()
        sys.exit(1)

    global VERBOSE
    VERBOSE = args.verbose

    global DRY_RUN
    DRY_RUN = args.dry_run

    args.func(args)


if __name__ == "__main__":
    main()
