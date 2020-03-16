#!/usr/bin/env python3

import inspect
import json
import os
import subprocess
import sys
import re
import collections

VERBOSE = True


def fatal(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def traced(fn, show_start=False, show_end=True):
    def inner(*args, **kwargs):
        if VERBOSE and show_start:
            print(f"Starting {fn.__name__}({args}, {kwargs})")
        ret = fn(*args, **kwargs)
        if VERBOSE and show_end:
            print(f"{fn.__name__}({args}, {kwargs}) = {ret}")
        return ret

    return inner


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
    """Gets the upstream branch tracked by `branch`.  If `branch` is none, uses the current branch."""
    if not branch:
        branch = "HEAD"
    branchref = git("rev-parse", "--symbolic-full-name", branch)
    return git("for-each-ref", "--format=%(upstream:short)", branchref)


def cmd_rebase():
    pass


def cmd_show():
    # Get the commits in this branch.
    upstream_branch = git_tracks()
    if not upstream_branch:
        fatal(
            "Set an upstream branch with e.g. `git branch --set-upstream-to origin/master`"
        )
    commits = git("log", "--pretty=%H", f"{upstream_branch}..HEAD").strip().split("\n")
    # Filter out empty commits (if e.g. the git log produces nothing).
    commits = [c for c in commits if c]
    if not commits:
        fatal("No commits in branch.  Is the upstream branch set correctly?")

    # TODO: Check for nonlinear history.

    # Get the commit message for each commit, keeping only the lines that start
    # with "GPC:".
    parsed_notes = {}
    for commit, notes in notes.items():
        lines = (
            re.sub("^git-pr-chain:", "", l)
            for l in notes.split("\n")
            if l.startswith("git-pr-chain:")
        )
        try:
            *_, last_line = lines
        except ValueError:
            continue
        parsed_notes[commit] = Notes(**json.loads(last_line))

    # TODO: Make this prettier.
    for c in commits:
        print(parsed_notes[c].pr, git("log", "--pretty=oneline", "-n1", c))


def cmd_upload():
    pass


def all_commands():
    # TODO: Switch to using inspect for this.
    return {
        "rebase": cmd_rebase,
        "show": cmd_show,
        "upload": cmd_upload,
    }


def main():
    all_commands()[sys.argv[1]]()


if __name__ == "__main__":
    main()
