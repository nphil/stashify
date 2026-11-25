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

1. Ship user-visible changes by **releasing, not just merging**: after the
   work is merged to `main`, dispatch `docker-publish.yml` on `main` with
   inputs `version: X.Y.Z` (no leading v) and `notes:` a human-readable
   changelog (short bullet list, user-facing wording). The Claude git proxy
   CANNOT push tags (403), so the dispatch path is the primary one; a
   human pushing an annotated `vX.Y.Z` tag works identically (tag message
   becomes the notes).
2. CI does the rest: builds+pushes the images (`X.Y.Z` **and `latest`** —
   `:latest` only moves on releases, never on plain main pushes) with the
   OCI version label set, creates the `vX.Y.Z` tag, and publishes a GitHub
   Release with the notes.
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
- The dashboard is the Svelte 5 + Vite app in `web/` (Lucent@2, 42 selectable
  palettes). `server.py` serves `web/dist` (`WEBUI_DIR` overrides it); the old
  `webui/` is gone. The Dockerfile builds it in a node stage and copies only
  `web/dist` into the python image, so node never ships. Local build:
  `cd web && npm ci && npm run build` (`npm run check` first). `web/dist`,
  `web/node_modules` and `web/shots` are gitignored. `web/BACKEND_NEEDS.md` is
  the contract with the server; the only place the app adapts to API shapes
  is `web/src/lib/api/cloudAdapter.ts`. Live updates come from `GET api/events`
  (SSE, `events.py`); behind a proxy it must not buffer (`X-Accel-Buffering: no`
  is sent; Caddy streams it by default).
- **Lucent card padding rule:** every card/panel has an inner inset of at least
  `--lu-inset-card` (20px, `web/src/styles/base.css`; `.panel.card` and `.job` apply it).
  Rows, text and controls sit inside that inset and never touch the card edge;
  separators may run full width, content may not. New card = use the token, not
  a literal. Check every new card at 390 and 1440 wide before shipping.
- Deployment target is the unRAID server `beastnas` (reachable via
  `tailscale ssh root@beastnas`; port-22 SSH is closed, Tailscale SSH is in
  check mode so the user may need to approve a login URL). Containers
  `Stashify` and `Stashify-Runner` are dockerMan template-managed;
  templates live in `unraid/` here and as `my-Stashify*.xml` on the
  server's flash at `/boot/config/plugins/dockerMan/templates-user/`.
