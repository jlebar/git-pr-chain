# git-pr-chain

`git-pr-chain` is an opinionated tool that manages dependent GitHub pull
requests.

Use `git-pr-chain` you will be able to:

- ✅ Create multiple Github PRs with 1 local git branch (each with one or more commits)
- ✅ Show the list of depedent PRs on the PR summary automatically
- ✅ Rebase all PRs on top of the latest changes with 1 command
- ✅ Update all PRs with one 1 command

`git-pr-chain` works if you use a rebase/rewrite-history workflow locally, i.e.
when you want to get new changes from upstream, you rebase onto the new master
rather than merging master into your branch.  The tool has no opinion on how PRs
should be *landed* -- you can merge them or rebase and commit them with or
without squashing.

To use `git-pr-chain`, create a branch with linear history (i.e. no merges).
From this branch we'll create a chain of dependent pull requests.  Each PR
contains one or more commits in your branch; you annotate the commits' commit
messages with `git-pr-chain: foo` to indicate which commits belong to which PRs.

`git-pr-chain` takes care of creating new PRs and updating existing ones when
you

 - add or remove commits,
 - rewrite history to modify a commit,
 - rewrite history to reorder PRs, or
 - merge a PR (and thus lose a PR in your chain).

## Example

```
$ touch foo && git add foo
$ git commit -m "Add foo\ngit-pr-chain: add-foo"

$ echo "blah blah" > foo
$ git commit -a -m "Update foo"

# We can also run `git-pr-chain new` to add PR-chain annotation automatically
$ touch bar && git add bar
$ git commit -m "Add bar"
$ git-pr-chain new

# We need to know what "upstream" is.
$ git branch --set-upstream-to origin/master

$ git-pr-chain show
Current branch is downstream from origin/master, 3 commit(s) ahead.

Github branch add-foo
  5bc1c5f Add foo
  c58163a Update foo
Github branch add-bar
  be6db17 (HEAD -> test) Add bar

$ git-pr-chain push
# Creates two PRs
#  - one for the two commits in add-foo, and
#  - one for add-bar

$ git-pr-chain merge --merge-method=rebase
# Merges "add-foo" (equivalent of clicking "merge" button on github)
```

## Setting up

### Grant Github access via `gh` or `hub`
   `git-pr-chain` reads your github oauth token from
   [`gh`](https://github.com/cli/cli) or [`hub`](https://github.com/github/hub),
   because oauth is hard.  So you'll need to sign in to github with one of those
   tools.

### Setting up `git-pr-chain` as an executable

Clone the repo and set it up as an executable
```
$ git clone git@github.com:jlebar/git-pr-chain.git
$ ln -s /path/to/git-pr-chain/git-pr-chain.py /usr/local/bin/git-pr-chain
```

## Usage

Run `git-pr-chain` to see available commands. Currently, the following commands are supported

```
    log                 List commits in chain
    new                 Mark HEAD as starting a new PR in the chain
    end-chain           Add a commit to mark the end of the chain
    push                Create and update PRs in github
    merge               Merge one (default) or more PRs (not yet implemented)
```

### Usage notes

 * If the `git-pr-chain:` annotation is missing, the commit will live on the
   same branch as the previous commit.  (The second commit in the example above
   takes advantage of this.)

 * If any commit message contains the string `git-pr-chain: STOP`, it and all
   further commits will not be pushed.

 * `git-pr-chain` adds info about the whole chain to each PR's description, but
   it won't (or, shouldn't!) overwrite changes you make to the PR outside of its
   `<git-pr-chain>` section.

 * You can add following config to add a prefix to the branches created by git-pr-chain
   `~/.gitconfig`:

   ```
   [pr-chain]
       branch-prefix = "something/"
   ```

   This way if you have a commit with `git-pr-chain: foo`, it will correspond to
   a remote branch named `something/foo`. Consider setting it to your
   username.

 * You can write `GPC:` instead of `git-pr-chain:` in commit messages, if you
   like.

## Known limitations

 * At the moment you need to `pip3 install pyyaml PyGithub`, because I don't
   understand Python packaging.

 * Your branches can't live in a fork; they must live in the same repo you're
   trying to push to (and therefore you must have permission to create branches
   in that repo).  This is a limitation of github, as far as I can tell.  Let me
   know if you know how to work around it.

 * If you merge many PRs in quick succession, Travis/CircleCI won't be able to
   keep up with the rapidly-changing branch bases and may send you many "build
   failed" emails.  CircleCI's seems to be less noisy in this failure mode than
   Travis; with Travis I once memorably got `O(n^2)` emails for a 20-PR chain.

## FAQ

### Why do you hate my merge-based workflow?

I don't.

### But I want to land PRs as merge commits upstream.

You can; the tool only requires that your *local* history has no merges.

## TODOs

 - Make this a proper Python package, and make an executable that doesn't end in
   `.py`.
 - Write tests.  I'm sure my code has no bugs.  :)
 - If this catches on, move into https://jazzband.co/?

## Special thanks

git-pr-chain is inspired by [git-pr-train](https://github.com/realyze/pr-train)
and [git-chain](https://github.com/Shopify/git-chain), and a tool written by
@mina86.
