#!/usr/bin/env python3

import argparse
import collections
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
from typing import List, Dict, Tuple, Iterable, Optional

VERBOSE = False


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
def git(*args, err_ok=False):
    """Runs a git command, returning the output."""
    stderr = subprocess.DEVNULL if err_ok else None
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
    # it matter?

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
            return matches[0].group(1)
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


@traced
def git_tracks(branch=None):
    """Gets the upstream branch tracked by `branch`.

    If `branch` is none, uses the current branch.
    """
    if not branch:
        branch = "HEAD"
    branchref = git("rev-parse", "--symbolic-full-name", branch)
    return git("for-each-ref", "--format=%(upstream:short)", branchref)


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
                to a linear history.
                """
            )
        )

    grouped_commits = itertools.groupby(commits, lambda c: c.gh_branch)

    # Count the number of times each github branch appears in grouped_commits.
    # If a branch appears more than once, that means we have an "AABA"
    # situation, where we go to one branch, then to another, then back to the
    # first.  That's not allowed.
    #
    # Ignore the `None` branch here.  It's allowed to appear at the beginning
    # and end of the list (but nowhere else!), and that invariant is checked
    # below.
    ctr = collections.Counter(branch for branch, _ in grouped_commits if branch)
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
                branches.
                """
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

              This shouldn't happen and is probably a bug in git-pr-chain.
              """
            )
        )


@traced
def branch_commits() -> List[Commit]:
    """Get the commits in this branch.

    The first commit is the one connected to the branch base.  The last commit
    is HEAD.
    """
    upstream_branch = git_tracks()
    if not upstream_branch:
        fatal(
            "Set an upstream branch with e.g. `git branch "
            "--set-upstream-to origin/master`"
        )
    commits = []
    for sha in (
        git("log", "--pretty=%H", f"{upstream_branch}..HEAD").strip().split("\n")
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
        f"Current branch is downstream from {git_tracks()}, "
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


def cmd_upload(args):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    subparser = parser.add_subparsers()

    sp_show = subparser.add_parser("show", help="List commits in chain")
    sp_show.set_defaults(func=cmd_show)

    sp_upload = subparser.add_parser("upload", help="Create and update PRs in github")
    sp_upload.add_argument("-n", "--dry-run", action="store_true")
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
    args.func(args)


if __name__ == "__main__":
    main()
