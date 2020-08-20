#!/usr/bin/env python3

"""
Manages chains of GitHub pull requests.
"""

# Requires
#  pip3 install pyyaml PyGithub

# Check with
#
#  $ black
#  $ pytype -d import-error

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
                if "hosts" in config:
                    return config["hosts"]["github.com"]["oauth_token"]
                if "github.com" in config:
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
    match = re.search(r"(?:[/:])([^/:]+/[^/:]+)(\.git)?$", remote_url)
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
        return bool(re.search(r"(git-pr-chain|GPC):\s*STOP\b", self.commit_msg))

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

    def print_shortdesc(self, prefix=""):
        if prefix:
            print(prefix, end="")
            sys.stdout.flush()
        # Call git log directly so we get nicely colorized output.
        subprocess.run(("git", "--no-pager", "log", "-n1", "--oneline", self.sha))

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
    upstream_branch = git("for-each-ref", "--format=%(upstream:short)", branchref)
    if not upstream_branch:
        fatal(
            "Set an upstream branch with e.g. `git branch "
            "--set-upstream-to origin/master`"
        )
    return upstream_branch


def git_upstream_remote(branch=None):
    # Get the name of the upstream remote (e.g. "origin") that this branch is
    # downstream from.
    return git_upstream_branch(branch).split("/")[0]


@functools.lru_cache()
@traced
def gh_branch_prefix():
    try:
        return git("config", "pr-chain.branch-prefix").strip()
    except subprocess.CalledProcessError:
        # git config exits with an error code if the config key is not found.
        return ""


def strip_gh_branch_prefix(branch: str) -> str:
    prefix = gh_branch_prefix()
    if branch.startswith(prefix):
        return branch[len(prefix) :]
    return branch


@functools.lru_cache()
def grouped_commits() -> List[Tuple[Optional[str], List[Commit]]]:
    # We have to group and then filter in separate steps because `cs` is a
    # one-time iterable, and both grouping and filtering needs to read it.
    res = (
        (gh_branch_prefix() + branch if branch else None, list(cs))
        for branch, cs in itertools.groupby(branch_commits(), lambda c: c.gh_branch)
    )
    return [(branch, cs) for branch, cs in res if branch and not cs[0].not_to_be_pushed]


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
        if c.gh_branch:
            continue
        if idx == 0:
            fatal(f"First commit ({c.sha}) must have a 'git-pr-chain:' annotation!")
        else:
            c.inferred_upstream_branch = commits[idx - 1].gh_branch

    validate_branch_commits(commits)
    return commits


def cmd_log(args):
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
            c.print_shortdesc(prefix="  ")


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
    # TODO: It would be nice to show merged PRs here too, rather than having
    # them disappear.

    chain = []
    for branch, _ in grouped_commits():
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

    # Show a warning not to click the "merge" button for everything other than
    # the first PR in the chain.
    do_not_merge_msg = textwrap.dedent("""\
    ⚠️⚠️ Please **do not click the green "merge" button** unless you know what
    you're doing.  This PR is part of a chain of PRs, and clicking the merge
    button will not merge it into master. ⚠️⚠️ """)
    is_first_pr = gh_branch == grouped_commits()[0][0]

    return f"""\
<git-pr-chain>

#### Commits in this PR
{commits_str}

#### [PR chain](https://github.com/jlebar/git-pr-chain)
{chain_str}

{do_not_merge_msg if not is_first_pr else ""}
</git-pr-chain>
"""


def get_open_prs() -> Dict[str, List[github.PullRequest.PullRequest]]:
    """Open PRs indexed by HEAD branch."""
    # Nominally you can ask the GH API for all PRs whose head is a particular
    # branch.  But this filter doesn't seem to do anything.  So instead, we
    # just pull all open PRs and filter them ourselves.
    #
    # TODO: If this is slow, we can kick it off asynchronously, before
    # push_branches.
    open_prs = collections.defaultdict(list)
    for pr in gh_repo_client().get_pulls(state="open"):
        open_prs[pr.head.ref].append(pr)
    return open_prs


def fatal_multiple_prs_for_branch(
    branch: str, prs: List[github.PullRequest.PullRequest]
) -> None:
    joined_pr_urls = "\n".join("  - " + pr.html_url for pr in prs)
    fatal(
        textwrap.dedent(
            f"""\
            Branch {branch} has multiple open PRs:
            {joined_pr_urls}
            Don't know which to choose!"""
        )
    )


def set_pr_bases_to_master(args):
    # Handles the possibility that PRs may be reordered.
    #
    # Consider branches' current bases (e.g. A -> B -> C) and the bases in
    # their PRs (e.g. A -> C -> B).  Consider this as one big graph and find
    # cycles, here B -> C -> B.  All commits which participate in a cycle must
    # have their base branch set to `master` before we can push branches to
    # github.  This prevents github from closing PRs incorrectly.
    #
    # (I think this is a little aggressive, but I am still not sure exactly
    # what the criterion should be.  In any case, it's perfectly safe to reset
    # all commits' upstream to master; the only issue is noise in the PR log.)
    repo = gh_repo_client()
    open_prs = get_open_prs()

    for branch, _ in grouped_commits():
        branch_prs = open_prs[branch]
        if len(branch_prs) > 1:
            fatal_multiple_prs_for_branch(branch, branch_prs)

    upstream_master = "".join(git_upstream_branch().split("/")[1:])
    seen_branches = []
    branches_to_reset = set()
    for branch, _ in grouped_commits():
        if not open_prs.get(branch):
            continue
        pr = open_prs[branch][0]
        try:
            idx = seen_branches.index(pr.base.ref)
            if idx != len(seen_branches) - 1:
                branches_to_reset.update(seen_branches[idx + 1 :])
                branches_to_reset.add(branch)
        except ValueError:
            pass

        seen_branches.append(branch)

    for branch in branches_to_reset - {upstream_master}:
        print(f"Temporarily resetting base of {branch} to {upstream_master}.")
        pr = open_prs[branch][0]
        if not DRY_RUN:
            pr.edit(base=upstream_master)


def create_and_update_prs(args):
    repo = gh_repo_client()
    open_prs = get_open_prs()

    # Check that every branch has zero or one open PRs.  Otherwise, the
    # situation is ambiguous and we bail.
    for branch, _ in grouped_commits():
        branch_prs = open_prs[branch]
        if len(branch_prs) > 1:
            fatal_multiple_prs_for_branch(branch, branch_prs)

    def base_for(idx) -> str:
        if idx == 0:
            return "".join(git_upstream_branch().split("/")[1:])
        base, _ = grouped_commits()[idx - 1]
        return base

    # Create new PRs if necessary.
    for idx, (branch, cs) in enumerate(grouped_commits()):
        branch_prs = open_prs[branch]
        if branch_prs:
            continue

        base = base_for(idx)
        print(f"Creating PR for {branch}, base {base}...")
        if not DRY_RUN:
            # Eh, using the first commit's title as the title of the PR isn't
            # great, but maybe it's OK as a guess.  Or maybe TODO: open an
            # editor?
            title = cs[0].commit_msg.split("\n")[0]
            pr = repo.create_pull(title=title, base=base, head=branch, body="")
            branch_prs.append(pr)
            # TODO: Auto-open this URL.
            print(f"Created {pr.html_url}")

    # Update PRs for each branch.
    for idx, (branch, cs) in enumerate(grouped_commits()):
        branch_prs = open_prs[branch]
        if len(branch_prs) != 1:
            fatal(
                f"Expected one open PR for {branch}, but was {len(branch_prs)}. "
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

        if not DRY_RUN:
            # chain_desc_for doesn't work in dry runs, because we don't create
            # PRs for it to reference.
            body = re.sub(
                r"\s*<git-pr-chain>.*</git-pr-chain>",
                chain_desc_for(branch, cs, open_prs),
                body,
                flags=re.DOTALL,
            )

            if base != pr.base.ref or body != pr.body:
                pr.edit(base=base, body=body)


def cmd_push(args):
    # If the user reordered PRs in their local tree, force-pushing those
    # commits can cause github to close the relevant PRs as "no commits here".
    # That's quite bad!  Easiest way I can see to avoid this is, if one PR's
    # base changes, set the base of that PR and all PRs after it to `master`.
    # We'll then update the bases in create_and_update_prs.
    set_pr_bases_to_master(args)
    push_branches(args)
    create_and_update_prs(args)


def cmd_merge(args):
    # Like in cmd_push, we set every PR's base branch to master, otherwise
    # push_branches() can cause github to close PRs incorrectly when we simply
    # want to be reordering them.
    set_pr_bases_to_master(args)
    push_branches(args)

    # create_and_update_prs() is important; it's what's responsible for
    # updating base branches.  Without this, we might merge into the previous
    # feature branch rather than master!
    create_and_update_prs(args)

    try:
        branch, cs = grouped_commits()[0]
    except IndexError:
        fatal("No commits to push!")

    prs = get_open_prs()[branch]
    if not prs:
        fatal(f"No open PR for branch {branch}")
    if len(prs) > 1:
        fatal_multiple_prs_for_branch(branch, prs)
    pr = prs[0]

    print(f"Will merge PR {pr.number} with method {args.merge_method}")
    for c in cs:
        c.print_shortdesc(prefix="  ")

    if DRY_RUN:
        print("DRY RUN: Not merging.")
        return

    if args.yes:
        yorn = "y"
    else:
        yorn = input("Continue [yN]? ")

    if yorn.lower() != "y" and yorn.lower() != "yes":
        return

    res = pr.merge(merge_method=args.merge_method)
    if not res.merged:
        print(f"Failed: {res.message}")
        return

    print("Merged!")
    if args.no_pull:
        return

    print("Pulling merged changes...")
    git(
        "pull",
        "--rebase",
        git_upstream_remote(),
        "".join(git_upstream_branch().split("/")[1:]),
    )

    # TODO: Push to github again so everything's updated.  Sadly can't just
    # call push_branches() and create_and_update_prs() because those rely on my
    # evil global caches, which are now out of date because we've merged our
    # PR!


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true")
    subparser = parser.add_subparsers()

    sp_log = subparser.add_parser("log", help="List commits in chain")
    sp_log.set_defaults(func=cmd_log)

    sp_push = subparser.add_parser("push", help="Create and update PRs in github")
    sp_push.set_defaults(func=cmd_push)

    sp_merge = subparser.add_parser(
        "merge", help="Merge one (default) or more PRs (not yet implemented)"
    )
    sp_merge.add_argument(
        "--merge_method",
        type=str,
        default="merge",
        choices=["merge", "squash", "rebase"],
    )
    sp_merge.add_argument("--yes", "-y", action="store_true")
    sp_merge.add_argument("--no-pull", help="Don't git pull after a successful merge.")
    sp_merge.set_defaults(func=cmd_merge)

    def cmd_help(args):
        if "command" not in args or not args.command:
            parser.print_help()
        elif args.command == "log":
            sp_log.print_help()
        elif args.command == "push":
            sp_push.print_help()
        elif args.command == "merge":
            sp_merge.print_help()
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
