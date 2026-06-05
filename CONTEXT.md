# Session handoff — hauler-airgap-updater

> Compressed context from the prior Claude Code session (session `0a8fb017`, run on
> the jump host `/home/daniel/hauler-airgap-updater`). Carried over to `hauler.ham.lan`
> so work can continue here. The full raw transcript is also on this VM under
> `~/.claude/projects/-home-dmeiselman-hauler-airgap-updater/` — run `claude --resume`
> in this directory to replay it; this file is the fast read.

## What we're building

A single-file Python tool (`airgap_update.py`, **zero third-party deps**) that updates
**in-place RKE2 clusters across a hard air gap** using hauler / dnf / `tar.zst`. It
identifies the upgrade artifacts (RPMs, images, helm charts), pulls them from the
internet, and bundles them to carry across the gap. Loosely based on the original
`hauler_all_the_things.sh` bootstrap script, but for *updates*, not first install.
**Not** using the Rancher management stack. Two-store hauler topology (separate
build vs. serve), standalone hauler VM serves images+charts.

## Topology / environment (real hosts)

- **`hauler.ham.lan`** — THIS VM. Runs the *entire* internet-connected side
  (`discover` + `build` + `ingest`). Rocky 9.8, x86_64. `hauler v1.4.3`, `helm`,
  `kubectl`, `zstd` all installed. `/opt` is a second disk (`xvdb`, UUID-pinned in
  `/etc/fstab`). hauler at `/opt/hauler` (`work_dir`/`store_dir` in config).
- **`repo.ham.lan`** — existing dnf repo server the nodes already point at. RPM
  artifact (`el8/` + `el9/` subdirs) gets dropped here. No node repo edits needed.
- **`rocky9-rke2.ham.lan`** — live internet reference cluster + **validation guinea
  pig**. Currently `v1.33.12+rke2r2`, single node (dev). longhorn chart `1.10.1`.
  cert-manager NOT installed here. Needs a kubeconfig reachable from this VM (or
  `--kubeconfig <path>`).
- **Air gap** — completely disconnected, **no jump host**, media crosses one
  direction only. The air-gapped cluster's true current version **cannot be
  auto-discovered** — it's typed by hand into `state.json` (or `discover` is run
  in-gap and its `state.json` carried out). `el8` **and** `el9`, x86_64 only.
  **Prod = multiple servers + multiple agents** (dev here is single-node).

## Architecture — three subcommands

1. **`discover`** (online, vs. live cluster, read-only): `kubectl get nodes` →
   kubeletVersion → RKE2 version; `helm list -A -o json` → dep chart versions.
   Plans from the **lowest node version across ALL nodes** (servers + agents) so a
   lagging agent's full path is bundled; warns on version spread. Writes `state.json`.
2. **`build`** (online): confirms/overrides current version (for the hand-supplied
   air-gap case), pulls **latest-patch-per-minor** from RKE2's **channels endpoint**
   (`update.rke2.io/v1-release/channels`), picks target minor (`--target 1.34`),
   computes the **minor-level** upgrade path. Emits **two artifacts**:
   `rpms_<date>.tar.zst` (`el8/`+`el9/` for the dnf server) and
   `hauler_<date>.tar.zst` (images+charts via `hauler store save`). `--dry-run`
   emits `airgap_hauler.yaml` for review (last run: 75 images / 2 charts / 14 RPMs).
3. **`ingest`** (offline hauler VM): `hauler store load` + `store serve`
   (registry :5000, fileserver :8080), bounce services. **Hauler-only** now (RPMs go
   separately to the dnf server). Prints multi-node apply guidance.

## Key design decisions (the "why")

- **K8s can't skip minors** → compute & bundle every intermediate minor. Path also
  includes the **latest patch of the *current* minor first** (covers patch-drift:
  air-gap cluster may sit anywhere in `1.33.x`, e.g. `1.33.6` vs proxy `1.33.12`).
- **Minor-level path** → patch drift between proxy and air-gap cluster doesn't change
  the plan. Example shape: `1.33.x → 1.33.12 → 1.34.7 → … → target patch`.
- **Two-artifact split** removed a pile of complexity: RPMs ride to the existing dnf
  server, hauler serves only images+charts. No `createrepo`/per-EL/node-repo edits.
- **state.json is discovered, not hardcoded**; committed as the auditable version
  record. Air-gap targets are typed in by hand; `build` confirms/overrides.
- **RKE2's dnf upgrade only covers RKE2's *bundled* components** (canal/calico,
  ingress-nginx, coredns, metrics-server, snapshot-controller). **longhorn &
  cert-manager are NOT bundled** — the tool *stages* their artifacts but does **not**
  drive their `helm upgrade`. That's a separate trigger.
- **Multi-node apply**: version-pinned `dnf install`, **one minor at a time
  cluster-wide, servers one-by-one** (wait Ready + etcd healthy) **then agents**. A
  blind `dnf update` is **unsafe** when the repo holds multiple minors (it jumps to
  the newest, skipping intermediates).
- **hauler v1.4.3 CLI reconciled** (verified against installed binary): `store sync
  -f`, `store save -f` (default `haul.tar.zst`), `store serve registry|fileserver -s`
  all ✓. **`store load` takes `-f`, NOT a positional arg** — that was a bug, fixed.
  Pass `-s/--store` explicitly (don't rely on `HAULER_STORE_DIR`). Charts sync as
  **OCI artifacts on the registry (:5000)**, not the fileserver. `store save` does its
  own zstd (VM needs `zstd` only for the *build* RPM tar).

## Validation strategy (§2a in DESIGN.md)

`hauler.ham.lan` is the whole internet side and a **self-contained dry run**: before
anything crosses the gap, actually drive `rocky9-rke2.ham.lan` `1.33.12 → 1.34.8` off
`repo.ham.lan` (RPMs) + `hauler.ham.lan` (images), confirm the node returns `Ready`.
Air-gap carry happens **only after** that validation passes.

## Status (as of handoff)

- ✅ **`discover`** — ran against live cluster, read `v1.33.12+rke2r2` + longhorn
  `1.10.1`, wrote `state.json`.
- ✅ **`build --dry-run`** — resolved `1.33.12 → 1.34.8` from real channels endpoint,
  valid `airgap_hauler.yaml`, all four artifact-URL classes HEAD-checked **HTTP 200**.
- ✅ Version logic (sort key, minor path planning, patch-drift, multi-node lowest).
- ✅ Added subprocess `timeout=` defenses (an unguarded `helm template` once hung
  ~10 min) + `--no-helm-images` escape hatch.
- ❌ **Never executed**: real `build` (download RPMs, `hauler store sync`/`save`),
  and **all** of `ingest`. Code is ~90% written, ~40% verified. **The gate to MVP was
  having hauler on this VM — which is now installed.**

## Next steps on this VM

1. `python3 airgap_update.py discover` → regenerate `state.json` from
   `rocky9-rke2.ham.lan` (confirm from this VM's vantage; needs kubeconfig).
2. `python3 airgap_update.py build --dry-run --target 1.34` → eyeball plan/manifest.
3. `python3 airgap_update.py build --target 1.34` → **first real** `hauler store
   sync`/`save`; produces `rpms_<date>.tar.zst` + `hauler_<date>.tar.zst`.
4. `ingest` → load+serve; then point `rocky9-rke2` at the repos and run the
   version-pinned upgrade to **validate the path**.

## Files & git

- **Tracked**: `airgap_update.py`, `config.json`, `state.json`, `DESIGN.md`,
  `README.md`, `.gitignore` (+ this `CONTEXT.md`).
- **Generated/ignored**: `_build/`, `*.tar.zst`, `airgap_hauler.yaml`,
  `__pycache__/`, `state.discovered.json`.
- The canonical git repo (branch `master`, initial commit) lived on the jump host.
  **This VM copy had no `.git`** — `git init` here if you want it self-contained.
- DESIGN.md is the living plan (§2a strategy, §4 mental model, §8a test env, §9
  runbook stretch, §10 management-tooling stretch, §11 changelog).

## Stretch goals (DESIGN.md §9–§10, deferred)

- **§10 management-tooling bundle**: `kubectl`, `hauler`, `zarf`, `uds`, `k9s` (+
  candidates: `jq`/`yq`/`stern`/`kubectx`/`kubens`/`cosign`/`crane`/`skopeo`/
  `etcdctl`/`velero`/`popeye`/`kubescape`/`trivy`). Ride as hauler Files via a
  `build --with-tools` flag. Self-hosting `hauler` matters most (rebuild the serving
  VM in-gap). Concern: integrity (sha256/cosign, no in-gap re-fetch) + arch matrix.
- **§9 runbook**: emit literal per-minor, per-node version-pinned upgrade commands
  (the tool already knows the path/versions — strong prod candidate).
