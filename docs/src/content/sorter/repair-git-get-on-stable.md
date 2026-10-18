---
layout: default
title: Repair git and get on stable
type: how-to
section: sorter
slug: sorter-repair-git-get-on-stable
kicker: SorterOS — Operations
lede: When Settings → Versions can't update the machine, put its software back on a release by hand from the Orange Pi's terminal.
permalink: /sorter/repair-git-get-on-stable/
---

The machine's software is a git checkout of this repository, sitting on a release tag. **Settings → Versions** in the UI moves it from one release to another. When that page fails, or the checkout is on a branch, has local edits, or git itself reports damage (often after a power cut), the commands below do the same steps by hand.

They are for a machine set up from the [SorterOS image]({{ '/sorter/installation/sorter-os/' | relative_url }}). Log in to the Orange Pi as `root` (over SSH, password `orangepi`, or with a screen and keyboard). On a machine you installed yourself, `cd` into your own checkout instead and restart the backend however you run it.

**Stop the machine before you start.** Step 8 restarts the backend.

## 1. See where the machine is

```bash
cd /home/orangepi/sorter-v2
git describe --tags --always
```

What it prints:

- `sorter/stable/v0.4.4` or `sorter/canary/v0.4.4`: on release 0.4.4. A release promoted to stable keeps its canary tag on the same commit, so a stable machine can print either.
- `sorter/canary/v0.4.14-7-g106a36861`: 7 commits past release 0.4.14, so on a branch such as `main`.
- A line starting `fatal:` or `error:`: git is broken. Go to [Repair git](#repair-git) first.

Keep the number in mind for step 3.

## 2. Download the releases

```bash
git fetch --tags --prune --prune-tags --force origin
```

Makes the machine's list of releases match GitHub's: new tags come in, deleted ones go, and a tag that moved on GitHub moves here too.

## 3. Pick the release

```bash
TAG=$(git tag -l 'sorter/stable/v*' --sort=-v:refname | head -1); echo "$TAG"
```

Prints the newest stable release, for example `sorter/stable/v0.4.4`, and keeps it in `$TAG` for the steps after. For the newest canary instead, put `canary` where it says `stable`.

<div class="callout callout-warning">
  <span class="callout-icon" aria-hidden="true">⚠</span>
  <p><strong>Don't go back to an older release.</strong> If step 1 printed a higher number than this one (a machine on canary, or on a branch), run this step again with <code>canary</code>. Newer code can change the machine's services and data in ways older code can't run on, which is why the UI never offers an older release either. The machine can come back to stable once stable catches up.</p>
</div>

## 4. Put local edits aside

```bash
git -c user.name=sorter -c user.email=sorter@localhost stash push -m "before $TAG"
```

Saves any edits to the software's own files in a git stash, so step 5 can't lose them. `No local changes to save` is the usual answer. The machine's settings (`machine.toml`, `.env`, the database) are not part of the checkout, and none of these steps touch them. The name and email only label the stash. `git stash list` shows what was saved.

## 5. Check out the release

```bash
git checkout -f --detach "refs/tags/$TAG"
```

Puts the software's files at that release. `--detach` leaves the checkout on the release itself rather than on a branch, which is how Settings → Versions tells which release the machine runs.

## 6. Install the Python packages

```bash
(cd software/sorter/backend && /usr/local/bin/uv sync --locked)
```

Makes the backend's Python environment match the release's lockfile. `--locked` stops with an error rather than change the lockfile.

## 7. Build the UI

```bash
(cd software/sorter/frontend && CI=true pnpm install --frozen-lockfile && pnpm build)
```

Installs the UI's packages and builds it. The backend serves this build. It takes a few minutes on the Orange Pi. `CI=true` keeps pnpm from waiting for an answer when it has to replace its packages folder.

## 8. Restart the backend

```bash
systemctl restart sorter-backend-dev.service
```

Starts the backend on the new release. `sorter-backend-dev.service` is the service the SorterOS image runs. A machine coming from release 0.3 or older takes about a minute longer: the new backend builds the UI once more, removes the separate UI service 0.3 ran, and restarts itself.

## 9. Check

```bash
git tag --points-at HEAD
journalctl -u sorter-backend-dev.service -n 30 --no-pager
```

The first lists the tags of the release from step 3. The second shows the backend's latest log lines, for when it doesn't come back. Then open [http://sorter.local](http://sorter.local): **Settings → Versions** shows the release.

## Repair git

Each of these stops the steps above. Fix it, then start again from step 1.

### `Unable to create '.../.git/index.lock': File exists`

A git command was cut off, often by a power cut, and left its lock behind. Check that no git is running (`pgrep -a git` prints nothing; an update started from the UI counts), then remove the lock:

```bash
rm -f /home/orangepi/sorter-v2/.git/index.lock
```

### `detected dubious ownership in repository`

Git refuses a checkout owned by another user. Trust this one:

```bash
git config --global --add safe.directory /home/orangepi/sorter-v2
```

### `you need to resolve your current index first`, or a merge or rebase in progress

Someone ran `git pull` or `git merge` on the machine and it stopped halfway. `git status` says which. Abort it:

```bash
git merge --abort
```

For a rebase, `git rebase --abort`.

### `object file ... is empty`, `loose object ... is corrupt`, `bad object HEAD`

Git's own files are damaged, usually by a power cut. `git fsck` lists the damage. Replace them with a fresh download. Only the `.git` folder is swapped, so the machine's settings stay where they are:

```bash
cd /home/orangepi
git clone --no-checkout --filter=blob:none https://github.com/basicallysource/sorter-v2 sorter-v2.new
mv sorter-v2/.git sorter-v2.git-broken
mv sorter-v2.new/.git sorter-v2/.git
rmdir sorter-v2.new
cd sorter-v2
```

Then do steps 3 and 5. Skip step 4: a damaged repository can't stash, and the fresh one doesn't know your edits. Step 5 overwrites edits to the software's own files, so copy out anything you changed first.

The fresh `.git` doesn't know which files the old release had, so any the new release dropped are left behind, and an old UI file can break the build in step 7. List them, then remove them:

```bash
git clean -nd
git clean -fd
```

`-n` only lists. Files git ignores stay, which is everything the machine writes: its settings, database, pictures and logs. A file you added yourself that git doesn't ignore is removed, so check the list first.

Then do steps 6, 7 and 8. Once the machine runs, delete the damaged copy:

```bash
rm -rf /home/orangepi/sorter-v2.git-broken
```
