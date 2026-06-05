# hauler-airgap-updater — Design & Plan

> Living document. Claude is authorized to keep this updated as the design and
> implementation evolve, without asking each time. Treat it as the source of
> truth for *intent*; the code is the source of truth for *behavior*.

Last updated: 2026-06-05 (session 2)

---

## 1. Problem

There are several **production, air-gapped RKE2 clusters** originally stood up
with a variant of `hauler_all_the_things.sh` (Rancher's hauler-based airgap
bootstrap), since tweaked by hand. They now need to be **updated in place** —
newer RKE2, newer RPMs, newer images, newer helm charts — without internet
access on the protected side.

We are **not** running the Rancher management stack. On the air-gapped side there
are **two pre-existing servers**: a **dnf repo server** (serves RPMs; cluster
nodes already point at it) and a **standalone hauler server** (serves container
images via an OCI registry on :5000 and helm charts/files on :8080). The "first
node" is not the hauler host.

## 2. Topology — two disconnected environments, two air-gap servers

There is **no jump host** and **no network path** between the two sides. The only
thing that crosses the gap is removable media (one direction: in). Any info that
must travel the other way (e.g. an air-gapped cluster's current version) crosses
by hand — a typed version string or a carried-out `state.json`.

`build` produces **two artifacts** that land on the **two existing air-gap
servers**:

```
   INTERNET SIDE                 ::: AIR GAP :::            AIR-GAPPED SIDE

  reference cluster                                    ┌────────────────────┐
  (PROXY/template,                  rpms_*.tar.zst     │  DNF repo server   │
   NOT the airgap cluster) ──┐   ┌───────────────────▶ │  (releasever-split;│
        ▲ discover (proxy)   │   │                      │   nodes already    │
        │ tmpl               │   │                      │   point here)      │
  ┌─────┴───────────┐        │   │                      └─────────┬──────────┘
  │  ONLINE build   │  carry │   │                                │ dnf
  │  (internet)     │  .tar  │   │                                ▼
  │  - resolve vers │  zst   │   │   hauler_*.tar.zst    ┌────────────────────┐
  │  - download RPMs│────────┴───┼────────────────────▶ │  HAULER server     │
  │  - sync images  │   ✈ air gap │   ingest:            │  images   :5000    │
  │  - 2 artifacts  │            │    load store +       │  charts   :8080    │
  └─────────────────┘            │    bounce services    └─────────┬──────────┘
                                 │                                  │ images/charts
  REAL airgap version conveyed   │                                  ▼
  across by hand (typed, or      │                       ┌────────────────────┐
  discover on the airgap side    └─────────────────────▶ │ RKE2 cluster nodes │
  + state.json carried out)         dnf update + restart │ (by hand, ordered) │
                                    rke2 (servers→agents) └────────────────────┘
```

- **RPM artifact** (`rpms_<date>.tar.zst`, top-level `el8/` + `el9/` dirs) →
  dropped on the **existing dnf repo server** (already releasever-split; nodes
  already point at it ⇒ **no node repo changes**). createrepo runs there
  (existing automation or by hand).
- **Hauler artifact** (`hauler_<date>.tar.zst`, images + charts) → loaded on the
  **hauler server** via `ingest`.

## 2a. Validation strategy — full internet-side dry run before the gap

The internet side is **not** just a build box — it is a complete, self-contained
rehearsal of the entire mechanism, run before anything crosses the air gap:

1. **Everything internet-connected runs on `hauler.ham.lan`**: `discover`,
   `build`, and `ingest` all execute there (it has internet, reaches the
   reference cluster, and hosts hauler). Build artifacts are served from this
   same box.
2. **The internet-connected cluster `rocky9-rke2.ham.lan` is the upgrade test
   subject.** We point it at `repo.ham.lan` (RPMs) + `hauler.ham.lan` (images)
   and actually perform the RKE2 upgrade (e.g. `1.33.12 → 1.34.8`), confirming
   the node returns `Ready` and the computed upgrade path works end to end.
3. **Only after that passes** do we carry the same artifacts across the air gap
   to the real disconnected clusters.

Implication: the tool must be **deployable + testable entirely on the
internet-connected side** with no air-gapped dependency. `hauler.ham.lan` needs
the build deps: `hauler`, `python3`, `tar` (present), plus `zstd`, `helm`, and
`kubectl` (for `discover` + verifying the upgrade).

## 3. Scope (decided)

**In scope**
- **RKE2 update**: `rke2-server` / `rke2-agent` / `rke2-common` / `rke2-selinux`
  RPMs (→ dnf server) **and** the matching airgap image set (→ hauler registry).
- **Staging helm-deployed deps** (longhorn, cert-manager, others we list): pull
  their charts + images into the hauler artifact so they're *available* offline.
  Over-including images for deps we don't run is fine ("doesn't hurt").
- **Upgrade-path awareness**: compute + pull every intermediate RKE2 minor
  required to walk current → target (k8s control planes can't skip minors).
- **One bundle set, all clusters** (clusters share a common version set).
- **Two-artifact flow**: `build` (online) → carry → RPMs to dnf server +
  `ingest` hauler artifact to hauler server.
- **Discovered, reviewable state** recording current + last-built versions.

**Explicitly out of scope (for now)**
- **No on-cluster orchestration.** The tool's job ends when artifacts are placed
  on the two air-gap servers. Operators run `dnf update` + restart rke2 on the
  nodes themselves, in order. (A generated runbook may come later.)
- **No driving of helm upgrades** for longhorn/cert-manager — stage only.
- **No Rancher management stack.**
- **No full-OS package mirroring** — only RKE2-related RPMs.
- **Management tooling bundle** (kubectl/hauler/zarf/uds/k9s) is a **stretch
  goal** (§10); for now those are brought across by hand as needed.

## 4. How the update actually applies (mental model)

- **RKE2's own bundled components** (canal/calico, ingress-nginx, coredns,
  metrics-server, snapshot-controller) ship *inside* the `rke2-server` RPM as
  HelmChart manifests + airgap images. When a node does `dnf update` (RPM from
  the dnf server) and the new `rke2-server` **service is restarted**, the
  embedded helm-controller reconciles those to the new versions. Images come
  from the hauler registry mirror (`/etc/rancher/rke2/registries.yaml`).
  - ⚠️ `dnf update` alone does **not** restart the service.
  - ⚠️ **Multi-node ordering** (prod has multiple servers + multiple agents):
    advance the cluster **one minor at a time**; within each minor, upgrade
    **control-plane servers one-by-one** (wait for `Ready` + etcd healthy before
    the next), **then agents one-by-one**. Finish a whole minor across all nodes
    before starting the next.
  - ⚠️ **Multi-minor bundles ⇒ no blind `dnf update`.** When the repo holds
    several minors, `dnf update` jumps to the newest minor and **skips
    intermediates** (violates the no-skip rule). Each step must be **version-
    pinned**: `dnf install -y rke2-{server,agent}-<ver> rke2-common-<ver>
    rke2-selinux-<sel>` then restart. (A single-minor bundle is the only case
    where blind `dnf update` is safe.)
- **Longhorn / cert-manager are NOT bundled.** `dnf` won't touch them. We only
  *stage* their artifacts; upgrading is a separate operator-driven `helm
  upgrade`.

## 5. Tooling decision

**Python 3, single file, standard library only.** Shells out to
`kubectl`/`helm`/`hauler`/`tar`; keeps the brains (semver, upgrade-path math,
interactive selection, state) in readable Python. Zero `pip install` on either
side. Chosen over Bash (too fragile for the version logic) and Ansible (node
config is out of scope; awkward at interactive prompts + version resolution).

## 6. Repo layout

```
hauler-airgap-updater/
├── DESIGN.md            # this file (living plan)
├── README.md            # how to run it
├── airgap_update.py     # the tool (discover + build + ingest)
├── config.json          # static: server paths, EL list, helm-dep list
└── state.json           # GENERATED by `discover`, then admin-edited
```

`state.json` is **not** shipped with placeholder versions — it's generated by
`discover` and reviewed/edited by the admin.

## 7. CLI shape

```
airgap_update.py discover [--kubeconfig PATH] [--state state.json]
airgap_update.py build    [--dry-run] [--current TAG] [--target MINOR]
                          [--deps-latest] [--no-input] [--no-helm-images]
                          [--state state.json]
airgap_update.py ingest   <hauler_bundle.tar.zst>
```

`--no-helm-images` skips `helm template` image resolution during build/dry-run
(faster iteration; charts are still staged, images for that dep are omitted).

### `discover` (against whatever cluster is reachable where it runs)
The two environments are fully disconnected, so `discover` only sees a cluster on
the *same* side it runs on:
- **Internet side** → queries the **reference cluster** (proxy/template; versions
  may differ from the air-gap targets). Good starting point, not authoritative.
- **Air-gapped side** (box that can reach the real cluster) → authoritative
  `cluster_current`, which must be **carried back by hand** to drive `build`.

Flow: read RKE2 from `kubectl get nodes` (kubeletVersion carries the rke2
suffix), read helm releases via `helm list -A -o json`, match against
`config.json` helm_deps, write `state.json`, remind admin to review/edit. Never
clobbers an existing `state.json` (writes `state.discovered.json` instead).

### `build` (internet side) — two artifacts
1. Preflight; load `state.json`.
2. Confirm/override the assumed **current** RKE2 version (`--current` or prompt;
   covers the hand-supplied air-gap value).
3. Fetch latest-patch-per-minor from the RKE2 **channels** endpoint
   (`update.rke2.io/v1-release/channels`).
4. **Interactive** (or `--target`) selection of the target minor.
5. Compute the **upgrade path** at *minor* granularity (absorbs patch drift):
   - **latest patch of the current minor** first (so a cluster on any older
     patch catches up before crossing a boundary),
   - then **latest patch of each intermediate minor**,
   - then the **target minor's latest patch**.
   Example: current `1.33.x`, target `1.34` → `1.33.12` then `1.34.8`.
6. Resolve helm-dep versions (prompt / `--deps-latest`), per `config.json`.
7. **RPM artifact**: download `rke2-{common,server,agent}` + `rke2-selinux` for
   each path version **× each EL** into `el8/`+`el9/` dirs; `tar -I zstd` →
   `rpms_<date>.tar.zst`.
8. **Hauler artifact**: write `airgap_hauler.yaml` (Images + Charts), `hauler
   store sync`, `hauler store save` → `hauler_<date>.tar.zst`.
   Build always syncs into a **fresh `_build/store`** (not the serving store),
   so the saved bundle contains only this build's artifacts — no accumulated
   content from prior syncs. The air-gap serving store may accumulate across
   ingests (old content stays available to nodes); the bundle you carry is always
   clean.
9. Update `state.last_build`; print carry instructions for both artifacts.

`--dry-run` writes the manifest + prints the RPM URL list, and stops before any
download / hauler call (so it runs fine without `hauler` installed).

### `ingest` (air-gapped hauler server) — hauler only
1. Preflight (root; `hauler`); locate bundle.
2. `hauler store load <bundle>` into the serving store.
3. Ensure `hauler@.service` unit; (re)start `hauler@registry` + `hauler@fileserver`.
4. Print `hauler store info` + operator reminder.

**RPMs are handled separately** on the dnf server: extract `rpms_<date>.tar.zst`
into the releasever-split repo base, then createrepo. No node repo edits.

## 8. State & config

- `config.json` — static knobs: server paths/ports, target cluster **`el_versions`**
  (`["el8","el9"]` — the *clusters'* ELs, not the build box's), platform/arch,
  `rke2-selinux` version, image-exclude patterns, helm-dep list (each with
  `chart_version_format`: `as-is`/`strip-v`), `build_dir`.
- `state.json` — current cluster versions (from `discover`) + a record of the
  last build (both artifact names, path, ELs, targets) for auditability.

## 8b. Environment facts

- Nodes are **EL8 and EL9, `x86_64` only** — no arm. Bundles carry **both** el8
  and el9 rke2 RPMs; images are EL-independent and shared.
- The **dnf repo server already exists and is releasever-split**: `repo.ham.lan`;
  nodes already point at it. The RPM artifact ships `el8/`+`el9/` dirs to drop in.
- The **hauler server**: `hauler.ham.lan` (being stood up) — serves images
  (:5000) + charts/files (:8080). `config.json: hauler.advertise_host`.
- Management tooling stays a stretch goal; brought over by hand for now.

## 8a. Test environment

- **Internet-connected cluster = upgrade test subject**: `rocky9-rke2.ham.lan`,
  **single control-plane node** (dev), RKE2 `v1.33.12+rke2r2`, longhorn chart
  `1.10.1`, no cert-manager. Used by `discover`, and **actually upgraded** as the
  validation gate (§2a) before any air-gap carry.
- **Dev vs prod topology**: dev is one node; **production clusters have multiple
  server (control-plane) nodes and multiple agent nodes**. The artifact side is
  node-count-agnostic; the multi-node concern lives entirely in the *apply
  ordering* (see §4) — servers one-by-one then agents, one minor at a time,
  version-pinned. `discover` plans from the lowest node version so a lagging
  agent is still covered.
- **`hauler.ham.lan`** — runs the **entire internet side** (`discover` + `build`
  + `ingest`). Installed: `hauler` v1.4.3 (`/usr/local/bin`, on PATH), `tar`,
  `curl`, `jq`, `python3`, `zstd`, `helm`, `kubectl`. `/opt` is a 300G xfs disk
  (`/dev/xvdb`, in fstab). All build deps present. ✅
- **Verified hauler v1.4.3 CLI** (reconciled into the code): `store sync -f
  <manifest> -s <store>`; `store save -f <out> -s <store>` (default
  `haul.tar.zst`); `store load -f <haul> -s <store>` (NOT positional);
  `store serve registry|fileserver -s <store>` (ports 5000/8080); `store info
  -s <store>`. Charts are served as OCI artifacts from the **registry (:5000)**,
  not the fileserver.

## 9. Known operational issues / gotchas

Encountered during first validation run on `hauler.ham.lan` → `rocky9-rke2.ham.lan`.
These are documented here so they don't bite the next operator.

- **DNS**: `hauler.ham.lan` must have an A record visible to cluster nodes before
  `registries.yaml` will resolve. Add via IPA or whatever DNS is in use.
- **Firewalld on the hauler VM**: ports **5000** (registry) and **8080**
  (fileserver) must be opened externally — hauler binds `*:port` but firewalld
  blocks them by default:
  ```
  firewall-cmd --add-port=5000/tcp --permanent
  firewall-cmd --add-port=8080/tcp --permanent
  firewall-cmd --reload
  ```
- **SELinux on the dnf repo server**: files extracted/copied into the nginx-served
  repo dir won't have the right SELinux context and nginx will return 403. After
  extracting RPMs and running `createrepo_c`, fix with:
  ```
  chcon -Rt httpd_sys_content_t <repo_dir>
  semanage fcontext -a -t httpd_sys_content_t '<repo_dir>(/.*)?'  # persistent
  restorecon -Rv <repo_dir>
  ```
  Note: if the mount point has no default SELinux policy (`restorecon` warns "no
  default label"), `chcon` is required — `restorecon` alone won't fix it.
- **`curl` exit-code trap**: `curl` returns 0 even on HTTP 404/403 unless `-f`/
  `--fail` is passed. A check like `curl ... && echo OK` is not reliable — always
  use `curl -sw '%{http_code}'` or `curl --fail` when scripting connectivity tests.
- **`tar` directory must pre-exist**: `tar -xf ... -C <dir>` fails if `<dir>`
  doesn't exist yet. `mkdir -p` before extracting.
- **`dnf check-update` exit codes**: exits 100 when updates are available (not an
  error), 0 when none, 1 on real error. Scripts wrapping it must handle 100 as
  success.
- **RPM NEVRA uses `~`, not `.`**: RKE2 package filenames use `.` as a separator
  (e.g. `rke2-server-1.34.8.rke2r2-0.el9.x86_64.rpm`) but the RPM metadata
  NEVRA uses `~` (e.g. `rke2-server-1.34.8~rke2r2-0.el9.x86_64`). `dnf install`
  requires the NEVRA form — `dnf install rke2-server-1.34.8.rke2r2-0.el9.x86_64`
  fails with "no match". Use `~` in the version string.

## 10. Open questions / future work

- **~~Emit an apply runbook~~** — DONE. `build` now generates `runbook_<date>.txt`
  alongside the artifacts: per-minor, per-EL, per-node copy-paste commands with
  `kubectl wait` checks between nodes. Printed at end of `build` and `ingest`.
  Node names come from `state.json` (reference cluster); operator substitutes real
  hostnames for air-gapped targets.
- **`rke2-selinux` versioning** is independent of RKE2; currently a config value.
  Confirm the right version / whether to auto-resolve.
- ~~Exact `hauler` subcommands/flags~~ — RESOLVED against hauler v1.4.3 and
  reconciled into the code (see §8a). Fixed a real bug: `store load` is `-f`, not
  positional. Re-verify if the hauler version changes.
- **helm binary version**: `include_helm_binary` (default off) grabs *latest*
  helm, which resolved to **v4.2.0**. If re-enabled, consider pinning helm 3.x.
- **Helm-dep upgrade paths**: longhorn (etc.) also can't skip minors. We stage
  only today; if we later drive upgrades, replicate the path logic.
- **RPM placement convenience**: optionally add a `place-rpms` helper that
  extracts the RPM tar into the dnf server's releasever dirs + createrepo, once
  we know the exact directory naming. For now: documented `tar -xf`.
- **Integrity**: capture sha256 / cosign for artifacts (no live re-fetch in-gap).

## 11. Stretch goals — management tooling bundle

Ship a curated set of **management binaries** so an operator on the disconnected
side has a working toolbox. Framed as stretch, but several are really hard deps
(e.g. `hauler` itself, so the offline serving VM can be rebuilt; `zarf`/`uds` if
those workflows are adopted).

### Candidate tools
- **Core / already implied**: `kubectl`, `helm`, `hauler` (self-host the tool
  that serves the bundle so the hauler VM can be rebuilt from scratch in-gap).
- **Requested**: `zarf` (Defense Unicorns packaging), `uds` (UDS CLI), `k9s`
  (cluster TUI).
- **Evaluate (with why each earns its place)**:
  - `jq` — JSON wrangling for scripts/`kubectl -o json`.
  - `yq` — YAML editing of manifests/values without a full editor.
  - `stern` — tail logs across many pods/containers at once.
  - `kubectx` / `kubens` — fast context/namespace switching.
  - `cosign` — verify signed images/artifacts offline (no live registry).
  - `crane` / `skopeo` — inspect/copy images against the hauler registry,
    debug pulls, retag/mirror without a daemon.
  - `etcdctl` — etcd snapshot backup/restore and health checks.
  - `velero` — cluster/PV backup & restore (pairs with longhorn).
  - `popeye` — cluster sanity/misconfig scanning, offline.
  - `kubescape` — security posture / CIS-style scanning, offline.
  - `trivy` — image/IaC vulnerability scanning against the in-gap registry.

### How they'd be bundled
- A `management_tools` section in `config.json`: name, version source (pin or
  "github latest"), asset-URL template, target platforms.
- `build` downloads them into (likely) a third small `tools_<date>.tar.zst`, or
  folds them into the hauler artifact as Files — TBD.
- Opt-in `build --with-tools` so routine bundles stay lean.

### Concerns
- **Platforms**: `linux/amd64` only in this environment (no arm, no darwin) — so
  the matrix is simple here.
- **Integrity**: sha256 / cosign (no live re-fetch in-gap).
- **Versioning**: record bundled tool versions in `state.last_build`.
- **Sourcing**: each project's release asset naming differs; verify per tool.

### Config sketch (not yet implemented)
```jsonc
"management_tools": {
  "platforms": ["linux/amd64"],
  "tools": [
    { "name": "kubectl", "version": "pin:v1.34.8",
      "url": "https://dl.k8s.io/release/{version}/bin/{os}/{arch}/kubectl" },
    { "name": "hauler", "version": "github:hauler-dev/hauler",
      "url": "https://github.com/hauler-dev/hauler/releases/download/{version}/hauler_{version}_{os}_{arch}.tar.gz" },
    { "name": "k9s", "version": "github:derailed/k9s",
      "url": "https://github.com/derailed/k9s/releases/download/{version}/k9s_{os}_{arch}.tar.gz" }
  ]
}
```

## 12. Changelog (most-recent first)
- 2026-06-05 (session 3) — Bug fixes from post-validation review: (1) hoisted
  `require(tar/zstd/hauler)` to top of `cmd_build` so missing tools fail before
  any downloads; (2) `render_runbook` now includes etcd health check after each
  control-plane node upgrade step; (3) OPERATOR.md Step 4c NEVRA corrected from
  `.` to `~` separator; (4) OPERATOR.md helm upgrade example corrected to OCI
  `oci://` scheme (not `--repo http://`); (5) `last_build.runbook` added to the
  state.json reference table.
- 2026-06-05 (session 2) — **Runbook generation implemented** (was §9 stretch
  goal). `build` writes `runbook_<date>.txt`: per-minor, per-EL, per-node
  version-pinned commands with `kubectl wait` guards. Printed in `--dry-run`,
  full `build`, and `ingest`. Node names from `state.json`; operator substitutes
  for air-gapped targets. `ingest` gains `--state` flag to find the runbook.
- 2026-06-05 (session 2) — Added §9 operational gotchas: DNS, firewalld ports,
  SELinux on nginx-served dirs, curl exit-code trap, tar pre-mkdir, dnf
  check-update exit 100. Documented from first real validation run.
- 2026-06-05 (session 2) — `build` end-of-run output now prints exact operator
  commands for RPM placement (tar extract, createrepo_c, chcon, semanage) with
  paths from config.json. No automation of repo server actions.
- 2026-06-05 (session 2) — config.json: added `dnf_repo.repo_dirs` mapping EL
  version to full absolute path on the repo server (flat, unambiguous).
- 2026-06-05 (session 2) — Documented two-store separation rationale: build uses
  a fresh `_build/store` per run (clean bundle, no accumulation from prior syncs);
  serving store accumulates across ingests. The separation also models the air-gap
  topology correctly even when build and ingest run on the same box.
- 2026-06-05 (session 2) — All build deps now installed on `hauler.ham.lan`
  (`zstd`, `helm`, `kubectl` + kubeconfig). Updated §8a.
- 2026-06-05 (session 2) — Added `--no-helm-images` flag: skips `helm template`
  image resolution for faster dry-run iteration; charts still staged. Updated §7.
- 2026-06-05 (session 2) — Added step-level verbose logging throughout `build`
  (fetch URLs printed, helm calls show timeout, GitHub API calls named). Diagnoses
  apparent hangs without altering behaviour.
- 2026-06-05 (session 2) — **`build --dry-run` validated end-to-end** on
  `hauler.ham.lan`: path `v1.33.12+rke2r2 → v1.34.8+rke2r2`, 75 images, 2
  charts, 14 RPMs (7 el8 + 7 el9), all URLs resolved. cert-manager v1.20.2,
  longhorn v1.12.0.
- 2026-06-05 (session 2) — Added `OPERATOR.md`: step-by-step guide covering
  discover → build (dry-run + full) → ingest → RPM extraction → ordered
  version-pinned node apply → air-gap carry. Includes troubleshooting section.
- 2026-06-05 — Initial design captured from three rounds of scoping Q&A.
- 2026-06-05 — Added `discover` subcommand: `state.json` generated from a
  reachable reference cluster (kubelet version + `helm list`), then admin-edited.
- 2026-06-05 — Implemented + validated `discover` against the reference cluster
  (`v1.33.12+rke2r2`, longhorn `1.10.1`, no cert-manager).
- 2026-06-05 — Clarified topology: the two sides are **fully disconnected** (no
  jump host, no network path); internet-side cluster is only a proxy/template.
- 2026-06-05 — Upgrade path is **minor-level** and includes the current minor's
  latest patch, absorbing patch drift between proxy and air-gap cluster.
- 2026-06-05 — Implemented `build` (dry-run validated): channels-based version
  resolution, target picker, path planner, helm-dep staging, manifest gen. All
  artifact URL classes HEAD-checked 200.
- 2026-06-05 — Environment: el8 **and** el9, x86_64 only, no arm. Bundles carry
  both ELs.
- 2026-06-05 — Added §10 Stretch goals: management tooling bundle (restored
  per-tool rationale notes for the candidate list).
- 2026-06-05 — Recorded air-gap server hostnames: dnf repo = `repo.ham.lan`
  (existing), hauler = `hauler.ham.lan` (being stood up). Set in config.json.
- 2026-06-05 — Reconciled code against **hauler v1.4.3** CLI; fixed `store load`
  (`-f`, not positional), added explicit `-s` to sync/save/load/info, corrected
  charts-served-via-registry messaging.
- 2026-06-05 — Added `run()` timeout support + bounded the helm calls (after a
  transient unguarded-subprocess hang).
- 2026-06-05 — **Strategy (§2a)**: the entire internet side runs on
  `hauler.ham.lan` (discover+build+ingest); the internet cluster
  `rocky9-rke2.ham.lan` is upgraded as a full validation gate before any air-gap
  carry. Tool must be deployable/testable entirely on the connected side.
- 2026-06-05 — **Multi-node** (prod = multiple servers + agents; dev = single
  node): `discover` now plans from the lowest node version; apply guidance fixed
  to version-pinned per-minor installs, servers one-by-one then agents (blind
  `dnf update` is unsafe with multi-minor bundles). Runbook stretch updated.
- 2026-06-05 — **Architecture change**: RPMs go to the existing
  **releasever-split dnf repo server**, images/charts to the **hauler server**.
  `build` now emits **two artifacts** (`rpms_*.tar.zst` with el8/el9 dirs +
  `hauler_*.tar.zst`) and downloads RPMs itself; `ingest` is **hauler-only**
  (no RPM/createrepo/per-EL/node-repo-edit logic). Nodes already point at the
  dnf server ⇒ no node repo changes. Updated topology, scope, flows, env.
