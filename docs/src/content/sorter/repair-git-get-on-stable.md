---
layout: default
title: Repair git and get on stable
type: how-to
section: sorter
slug: sorter-repair-git-get-on-stable
kicker: SorterOS — Operations
lede: Put the machine's software back on a release from the Orange Pi's terminal, whatever state it is in.
permalink: /sorter/repair-git-get-on-stable/
---

For when **Settings → Versions** can't update the machine: it offers no update, the update fails, or someone changed the software by hand.

## Run the script

Log in to the Orange Pi (over SSH: user `root`, password `orangepi`) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/basicallysource/sorter-v2/main/software/scripts/get-on-release.sh | sudo bash
```

That puts it on the newest stable release. For canary:

```bash
curl -fsSL https://raw.githubusercontent.com/basicallysource/sorter-v2/main/software/scripts/get-on-release.sh | sudo bash -s canary
```

It takes a few minutes and restarts the backend at the end, so stop the machine first.

**Nothing on the machine is lost.** Its settings, database, pictures and logs are not touched. Edits to the software's own files, and commits made on the machine, are saved on a git branch named `backup/<date>`, and any other file the release would replace is copied to a backup folder beside the checkout. The script prints where each went.

It also clears what stops an update: a branch instead of a release, a merge stopped halfway, a stale git lock, damaged git files.

It won't move the machine back to an older release (from canary to an older stable, say), since newer software can change the machine in ways older software can't run on. Use canary until stable catches up.

## By hand

The same main steps, on a checkout in good shape:

1. Download the releases and put local edits aside.
   ```bash
   cd /home/orangepi/sorter-v2
   git fetch --tags --prune --prune-tags --force origin
   git -c user.name=sorter -c user.email=sorter@localhost stash
   ```
2. Check out the newest stable release (put `canary` in place of `stable` for canary).
   ```bash
   git checkout -f --detach "$(git tag -l 'sorter/stable/v*' --sort=-v:refname | head -1)"
   ```
3. Install the Python packages and build the UI.
   ```bash
   (cd software/sorter/backend && uv sync --locked)
   (cd software/sorter/frontend && CI=true pnpm install --frozen-lockfile && pnpm build)
   ```
4. Restart the backend.
   ```bash
   systemctl restart sorter-backend-dev.service
   ```
