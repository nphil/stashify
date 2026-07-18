# decensor-engine (vendored Jasna)

`decensor-engine/` is a **git submodule** pointing at a **private** working copy of
[Jasna](https://github.com/Kruk2/jasna) (`nphil/decensor-engine`) — the decensor
engine Stashify's Windows runner drives. It's here so the engine can be modified and
optimized over time; the runner still executes the built `jasna.exe`, this is the
source behind it.

- **Upstream:** `Kruk2/jasna` (AGPL-3.0).
- **This fork:** private, full history + tags mirrored from upstream.
- **Licensing:** the submodule's contents are **AGPL-3.0** (upstream's license, which
  travels with it). Because it's a *submodule*, none of that source lives in this
  (public) Stashify tree — only a commit pointer + `.gitmodules`. Keep it that way:
  don't copy Jasna source files up into the Stashify repo.

## Get the code (after cloning Stashify)

```sh
git submodule update --init decensor-engine     # needs access to the private repo
# or clone Stashify with:  git clone --recurse-submodules <stashify-url>
```

## Sync with upstream Jasna

```sh
cd decensor-engine
git remote add upstream https://github.com/Kruk2/jasna.git   # one-time
git fetch upstream
git merge upstream/main          # or: git rebase upstream/main
git push origin main             # update your private fork
cd ..
git add decensor-engine          # record the bumped pointer in Stashify
git commit -m "Bump decensor-engine to latest upstream"
```

Upstream was at **v0.8.0** when this was vendored (the runner ships `jasna.exe` v0.7.2).
