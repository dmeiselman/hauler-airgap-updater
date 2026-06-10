# Operator Guide — hauler-airgap-updater

> Step-by-step procedure for building and delivering RKE2 update bundles across
> an air gap. See DESIGN.md for architecture and rationale.

---

## Overview

The tool's job ends at **downloading**. From an internet-connected build box
(`hauler.ham.lan`) `build` produces **two artifacts** plus a generated
**`RUNBOOK.md`**; loading the artifacts and applying the upgrade on the nodes
are manual steps the runbook spells out (there is **no `ingest` subcommand**).

| Output | Where it goes | What it feeds |
|---|---|---|
| `rpms_<date>.tar.zst` | DNF repo server (`repo.ham.lan`) | `dnf install` on each node |
| `hauler_<date>.tar.zst` | Hauler server(s) (`hauler.ham.lan`) | container image pulls + helm charts |
| `RUNBOOK.md` | stays on the build box (copy/paste from it) | the exact, version-pinned load + apply commands |

Node repo config does **not** change — nodes already point at both servers.
`RUNBOOK.md` is generated for the precise upgrade path and EL releases this
build covers; copy commands straight out of it. This guide is the *narrative*
around that generated runbook.

---

## Prerequisites

### Internet-connected build box (`hauler.ham.lan`)

| Tool | Notes |
|---|---|
| `python3` | stdlib only, no pip install |
| `hauler` v1.4.3 | `/usr/local/bin/hauler` — used for `store sync`/`store save` |
| `helm` | chart image resolution |
| `kubectl` + kubeconfig | `discover` subcommand |
| `tar` | present by default |
| `zstd` | needed for the RPM tar (`tar -I zstd`) |

`build` fails fast: `tar`, `zstd`, and `hauler` are all checked before any
download starts, so a missing binary won't waste a partial build.

### Cluster access
- Kubeconfig for the reference cluster (`rocky9-rke2.ham.lan`) at `~/.kube/config`
  or passed via `--kubeconfig`.
- For air-gapped clusters: the current RKE2 version must be obtained by running
  `discover` on the air-gap side and carrying `state.json` out by hand, **or**
  typed in manually via `--current`.

---

## Step 1 — Discover current versions

Run from `hauler.ham.lan` against the reference (internet-connected) cluster:

```bash
python3 airgap_update.py discover
```

This queries `kubectl get nodes` and `helm list`, then writes `state.json`.

If `state.json` already exists, a fresh copy is written alongside it (e.g.
`state.discovered.json`) instead of overwriting — diff and merge by hand:

```bash
diff state.json state.discovered.json
# edit state.json as needed, then remove the discovery copy
```

**For air-gapped clusters:** the version written by `discover` reflects the
internet-side reference cluster (which may be newer). Either:
- Pass `--current v1.33.6+rke2r1` at build time to override, **or**
- Run `discover` on the air-gap side against the real cluster, carry
  `state.json` out on media, and use that as the build input.

---

## Step 2 — Dry-run (verify the plan before downloading)

```bash
python3 airgap_update.py build --dry-run --target 1.34 --deps-latest --no-input
```

This prints:
- The upgrade path (e.g. `v1.33.12+rke2r2 -> v1.34.8+rke2r2`)
- All RPM URLs it would download
- Writes `airgap_hauler.yaml` for review

Nothing is downloaded or sent to hauler. Use this to sanity-check the plan
before committing to a full build.

**Useful flags:**

| Flag | Effect |
|---|---|
| `--target 1.34` | Target minor version (skips interactive prompt) |
| `--deps-latest` | Use latest GitHub release for each helm dep |
| `--no-input` | Non-interactive; accept all defaults |
| `--current v1.33.6+rke2r1` | Override assumed current version (for air-gap targets) |

---

## Step 3 — Full build (downloads + hauler store + runbook)

```bash
python3 airgap_update.py build --target 1.34 --deps-latest --no-input
```

This will:
1. Download all RPMs into `_build/rpms/el8/` and `_build/rpms/el9/`
2. Create `rpms_<date>.tar.zst` (top-level `el8/` + `el9/` dirs)
3. Run `hauler store sync` to pull images + charts (~75 images, takes 10–30 min)
4. Run `hauler store save` to create `hauler_<date>.tar.zst`
5. Update `state.json` with last-build metadata
6. Write **`RUNBOOK.md`** — the exact, version-pinned load + apply commands

Disk requirements: allow ~5 GB for the hauler store, ~500 MB for RPMs.

Regenerate the runbook any time from the recorded build:

```bash
python3 airgap_update.py runbook
```

---

## Step 4 — Validate on the internet-connected cluster (before crossing the gap)

Load the artifacts into hauler and actually upgrade `rocky9-rke2.ham.lan` first.
This confirms the upgrade path works end to end before media crosses the air gap.
The commands below are exactly what `RUNBOOK.md` generates — substitute the
versions it printed for your build.

### 4a — Load the hauler bundle on `hauler.ham.lan` (as root)

```bash
sudo hauler store load -f hauler_<date>.tar.zst -s /opt/hauler/store
sudo systemctl restart hauler@registry hauler@fileserver
hauler store info -s /opt/hauler/store   # verify contents
```

- `hauler@registry` — OCI registry at `:5000` (images + helm charts)
- `hauler@fileserver` — HTTP fileserver at `:8080` (files; empty for standard builds)

### 4b — Extract RPMs on `repo.ham.lan`

Copy `rpms_<date>.tar.zst` to the dnf repo server, then:

```bash
# the bundle holds one dir per EL (el8/, el9/) at the top level
tar -xf rpms_<date>.tar.zst -C /path/to/repo/base/
createrepo_c --update /path/to/repo/base/el8/
createrepo_c --update /path/to/repo/base/el9/
```

### 4c — Upgrade the reference cluster (version-pinned, ordered)

> The repo now holds **multiple minors**. Do **not** use `dnf update` — it will
> skip intermediates and violate the Kubernetes no-skip rule. Pin every step to
> the exact version `RUNBOOK.md` prints.

For each minor in the upgrade path (e.g. first `1.33.12`, then `1.34.8`), pin the
RKE2 version token (`<base>.<rke2r>`, e.g. `1.33.12.rke2r2`) and the selinux
package (`rke2-selinux-<selinux_version>`, e.g. `rke2-selinux-0.21`):

**On each control-plane server, one at a time:**

```bash
sudo dnf install -y rke2-server-1.33.12.rke2r2 rke2-common-1.33.12.rke2r2 rke2-selinux-0.21
sudo systemctl restart rke2-server

# wait until this node is Ready:
kubectl get nodes
# and etcd is healthy before moving to the next server:
kubectl -n kube-system exec -it etcd-<node> -- etcdctl endpoint health \
  --cacert /var/lib/rancher/rke2/server/tls/etcd/server-ca.crt \
  --cert   /var/lib/rancher/rke2/server/tls/etcd/server-client.crt \
  --key    /var/lib/rancher/rke2/server/tls/etcd/server-client.key
```

**Then, on each agent node, one at a time:**

```bash
sudo dnf install -y rke2-agent-1.33.12.rke2r2 rke2-common-1.33.12.rke2r2 rke2-selinux-0.21
sudo systemctl restart rke2-agent
# wait for Ready before moving to the next agent
```

Finish every node on one minor before starting the next.

> **Heads-up (multi-server):** `systemctl restart` is the cutover for that node,
> but the cluster's bundled components (coredns, canal, ingress-nginx,
> metrics-server, …) are reconciled cluster-wide. Until a **majority of servers**
> are on the new version, the lagging servers drag those component versions back
> down — a half-finished step can appear to "downgrade" minutes later. Don't stop
> partway through a step.

---

## Step 5 — Carry artifacts across the air gap

Once the internet-side upgrade validates (node `Ready`, workloads healthy):

```
rpms_<date>.tar.zst   →  DNF repo server (releasever-split repo base)
hauler_<date>.tar.zst →  Hauler server
```

Both files go on removable media in the same direction (one way: in). Take
`RUNBOOK.md` along too — it has the exact commands for this build.

---

## Step 6 — Load on the air-gapped hauler server

Repeat step 4a on the air-gapped `hauler.ham.lan` (as root), after copying the
bundle over:

```bash
sudo hauler store load -f hauler_<date>.tar.zst -s /opt/hauler/store
sudo systemctl restart hauler@registry hauler@fileserver
```

Then extract RPMs on the air-gapped `repo.ham.lan` as in step 4b.

---

## Step 7 — Apply the upgrade on the air-gapped cluster

Follow the same ordered, version-pinned procedure as step 4c. The versions in
the upgrade path are recorded in `state.json:last_build` and printed verbatim in
`RUNBOOK.md`.

---

## Helm-deployed dependencies (longhorn, cert-manager)

The hauler bundle **stages** these charts and images — it does **not**
automatically upgrade them (they aren't part of RKE2's bundled set). After the
RKE2 upgrade is complete:

```bash
# Charts are stored as OCI artifacts in the hauler registry (:5000).
# Check the exact OCI path with: hauler store info -s /opt/hauler/store
# Then upgrade using the oci:// scheme — NOT --repo http://

# longhorn example:
helm upgrade longhorn oci://hauler.ham.lan:5000/longhorn/longhorn \
  --namespace longhorn-system \
  --version 1.12.0
```

The OCI path prefix (e.g. `/longhorn/longhorn`) comes from `hauler store info`
output. Use the `oci://` scheme; `--repo http://` will not work for OCI-stored
charts.

---

## Reference: state.json fields

| Field | Set by | Purpose |
|---|---|---|
| `cluster_current.rke2` | `discover` (or hand-edit) | Starting version for upgrade-path planning |
| `cluster_current.helm_deps` | `discover` | Installed chart versions on the reference cluster |
| `cluster_current.nodes` | `discover` | Node inventory (roles + versions) |
| `last_build.rke2_path` | `build` | Exact upgrade path bundled |
| `last_build.rpm_bundle` | `build` | Filename of the RPM artifact |
| `last_build.hauler_bundle` | `build` | Filename of the hauler artifact |
| `last_build.els` | `build` | EL releases the RPM bundle covers |
| `last_build.targets` | `build` | Target versions (rke2 + helm deps) |

`runbook` reads `last_build` to regenerate `RUNBOOK.md` without re-downloading.

---

## Troubleshooting

**`helm template` hangs during build**
: It's downloading the chart index on first run. It has a 120 s timeout and will
  continue. Use `--dry-run` to iterate quickly on the plan without full downloads.

**`hauler store sync` is slow**
: Normal — it's pulling ~75 images from public registries. Allow 10–30 min
  depending on connectivity. Run on the build box, not your laptop.

**`hauler store load` fails with "no such file"**
: Pass the bundle path explicitly via `-f` and the serving store via `-s`
  (e.g. `-f hauler_<date>.tar.zst -s /opt/hauler/store`).

**Nodes not pulling images after load**
: Confirm `hauler@registry` is running (`systemctl status hauler@registry`) and
  that `/etc/rancher/rke2/registries.yaml` on the nodes points to
  `hauler.ham.lan:5000`.

**`dnf install` version not found**
: The RPM bundle may not have been extracted to `repo.ham.lan` yet, or
  `createrepo_c` hasn't run. Verify with `dnf repoquery rke2-server`.
