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
from typing import List, Dict, Tuple, Iterable

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
            ms = (starttime - datetime.datetime.now()) / one_ms
            print(f"{fn.__name__}({args}, {kwargs}) returned in {ms:.0}ms: {ret}")
        return ret

    return inner


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
    def __init__(self, sha: str):
        self.sha = sha
        self.upstream_branch = None  # TODO
        self.upstream_branch_inferred = False
        self.is_merge_commit = False  # TODO

    def shortdesc(self):
        # TODO
        return ""


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
        return list_strs(c.shortdesc() for c in cs)

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

    missing_upstream_branches = [c for c in commits if not c.upstream_branch]
    if missing_upstream_branches:
        fatal(
            textwrap.dedent(
                f"""\
                Unable to infer upstream branches for commit(s):

                {list_commits(missing_upstream_branches)}

                This shouldn't happen and is probably a bug in git-pr-chain.
                """
            )
        )

    # Ensure that no branch appears twice in the uinq'ed list.  That would mean
    # we have an "AABA" situation, which isn't allowed.
    ctr = collections.Counter(
        branch for branch, _ in itertools.groupby(commits, lambda c: c.upstream_branch)
    )
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
    commits = [
        Commit(sha)
        for sha in (
            git("log", "--pretty=%H", f"{upstream_branch}..HEAD").strip().split("\n")
        )
        # Filter out empty commits (if e.g. the git log produces nothing).
        if sha
    ]

    # Infer upstream branch names on commits that don't have one explicitly in
    # the commit message
    for idx, c in enumerate(commits):
        if idx == 0:
            if not c.upstream_branch:
                fatal(
                    "First commit in branch must have an explicit upstream "
                    "branch in commit message.  Rewrite history and add "
                    '"git-pr-chain: branchname" to commit message.'
                )
            continue
        if not c.upstream_branch:
            c.upstream_branch = commits[idx - 1].upstream_branch
            c.upstream_branch_inferred = True

    validate_branch_commits(commits)
    return commits


def cmd_show(args):
    commits = branch_commits()
    if not commits:
        fatal(
            "No commits in branch.  Is the upstream branch (git branch "
            "--set-upstream-to <origin/master or something> set correctly?"
        )

    # TODO: Make this prettier, e.g. break up by upstream branch.
    for c in commits:
        print(c.shortdesc())


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
