# Claude session notes

## Workflow preferences (from Nitin)

- **Merge to `main`**: once work on a feature branch is validated (builds
  pass, fix confirmed), merge it to `main` and push — don't leave finished
  work sitting on a branch waiting for a PR review. This is a standing
  instruction for all of Nitin's projects, given 2026-08-14.

## Release & changelog workflow (standing instruction, 2026-08-14)

Nitin runs the ShipLog plugin (junkerderprovinz/shiplog) on unRAID: it reads
the container image's `org.opencontainers.image.source` label, fetches this
repo's **GitHub Releases**, and shows the release notes as the changelog in
the Docker tab. Every image release must therefore ship with release notes:

1. Ship user-visible changes by **tagging, not just merging**: after the work
   is merged to `main`, create an **annotated tag** `vX.Y.Z` whose tag message
   is the human-readable changelog (short bullet list, user-facing wording),
   then push the tag.
2. CI (`docker-publish.yml`) does the rest: the v* tag builds+pushes the
   images (`X.Y.Z`, `X.Y`, **and `latest`** — `:latest` only moves on tags,
   never on plain main pushes) with the OCI version label set, and the
   `release` job publishes a GitHub Release from the tag message.
3. `make_latest: false` on app releases is deliberate — `/releases/latest`
   must keep resolving to the `winrunner-v*` release because
   `setup-stashify-runner.ps1` downloads
   `/releases/latest/download/stashify-winrunner.zip`.
4. Version line: app images use plain `vX.Y.Z` (started at v2.1.0);
   the Windows runner keeps its separate `winrunner-vX.Y.Z` series.

ShipLog facts (learned 2026-08-14 by reading its source):
- Changelog span filtering only works when releases are tagged
  plain `vX.Y.Z`/`X.Y.Z` (semver-parseable); prefixed series like
  `winrunner-v*` are correctly excluded from version spans.
- ShipLog marks any dockerMan-managed app absent from two consecutive
  Community Applications feed crawls as "Discontinued" — a false positive
  for personal apps that were never IN CA. No config/label opt-out exists
  (checked its full config surface). Cosmetic only.
- ShipLog hits the GitHub API anonymously (60 req/h per IP) unless the user
  puts a PAT in its settings (`GITHUB_TOKEN`); without one the changelog
  bubble may show a rate-limit note instead of notes.

## Project facts worth knowing

- Images publish to ghcr via `.github/workflows/docker-publish.yml` on every
  push to `main` (paths-ignore: docs/assets/unraid/markdown). They MUST be
  pushed with Docker media types (`oci-mediatypes=false`) — unRAID's Docker
  tab update checker 404s on bare OCI image manifests and shows Version
  "not available" (fixed 2026-08-14; don't regress this).
- Deployment target is the unRAID server `beastnas` (reachable via
  `tailscale ssh root@beastnas`; port-22 SSH is closed, Tailscale SSH is in
  check mode so the user may need to approve a login URL). Containers
  `Stashify` and `Stashify-Runner` are dockerMan template-managed;
  templates live in `unraid/` here and as `my-Stashify*.xml` on the
  server's flash at `/boot/config/plugins/dockerMan/templates-user/`.
