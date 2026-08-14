# Claude session notes

## Workflow preferences (from Nitin)

- **Merge to `main`**: once work on a feature branch is validated (builds
  pass, fix confirmed), merge it to `main` and push — don't leave finished
  work sitting on a branch waiting for a PR review. This is a standing
  instruction for all of Nitin's projects, given 2026-08-14.

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
