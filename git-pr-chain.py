#!/usr/bin/env python3

import argparse
import inspect
import json
import os
import subprocess
import sys
import re
import collections
import json
from typing import List, Dict, Tuple

from github import Github  # pip install PyGithub
from xdg import XDG_CONFIG_HOME  # pip install xdg

VERBOSE = False


def fatal(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def traced(fn, show_start=False, show_end=True):
    def inner(*args, **kwargs):
        if VERBOSE and show_start:
            print(f"Starting {fn.__name__}({args}, {kwargs})")
        ret = fn(*args, **kwargs)
        if VERBOSE and show_end:
            print(f"{fn.__name__}({args}, {kwargs}) = {ret}")
        return ret

    return inner


def gh_access_code():
    # TODO: Make this user-friendly and interactive.
    with open(os.path.join(XDG_CONFIG_HOME, "git-pr-chain.json"), "r") as f:
        return json.load(f)["access_code"]


def gh_session():
    return Github(gh_access_code())


@traced
def git(*args, err_ok=False):
    """Runs a git command, returning the output."""
    with open(os.devnull, "w") as devnull:
        stderr = devnull if err_ok else None
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


@traced
def git_tracks(branch=None):
    """Gets the upstream branch tracked by `branch`.

    If `branch` is none, uses the current branch.
    """
    if not branch:
        branch = "HEAD"
    branchref = git("rev-parse", "--symbolic-full-name", branch)
    return git("for-each-ref", "--format=%(upstream:short)", branchref)


@traced
def branch_commits() -> List[Tuple[str, Dict[str, str]]]:
    """Get the commits in this branch.

    Returns a list of (commit, {metadata_key, metadata_val}).
    """
    # TODO: Check for nonlinear history.

    upstream_branch = git_tracks()
    if not upstream_branch:
        fatal(
            "Set an upstream branch with e.g. `git branch "
            "--set-upstream-to origin/master`"
        )
    split_log = (
        git("log", "--pretty=%H", f"{upstream_branch}..HEAD").strip().split("\n")
    )

    # Filter out empty commits (if e.g. the git log produces nothing).
    return [(c, parse_md(c)) for c in split_log if c]


def parse_md(commit: str) -> Dict[str, str]:
    msg = git("show", "-s", "--format=%B", commit)
    lines = [
        re.sub(r"^GPC:\s*", "", l).strip()
        for l in msg.split("\n")
        if l.startswith("GPC:")
    ]

    md = {}
    for l in lines:
        if not l:
            # I guess we can ignore empty "GPC:" lines.
            continue

        # Format is "key value, which can have spaces or whatever"
        match = re.match(r"^(\w+)\s+(.*)$", l)
        if not match:
            warn(f"Error parsing line in {commit}: {l}")
            continue

        k, v = match.group(1, 2)
        # Right now, we only know how to parse "GPC:PR blah blah"
        if k != "PR":
            warn(f"Unexpected key {k} in {commit}: {l}")
            continue

        if k == "PR":
            # TODO Check that it's a URL of the right form.
            pass

        if k in md:
            warn(f"Duplicate key {k} in {commit}")

        md[k] = v

    return md


def cmd_show(args):
    commits = branch_commits()
    if not commits:
        fatal(
            "No commits in branch.  Is the upstream branch (git branch "
            "--set-upstream-to <origin/master or something>set correctly?"
        )

    # TODO: Make this prettier.
    for commit, md in commits:
        print(git("log", "--oneline", "-n1", commit) + " " + str(md))


def cmd_upload(args):
    g = gh_session()


def main():
    parser = argparse.ArgumentParser()
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
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
