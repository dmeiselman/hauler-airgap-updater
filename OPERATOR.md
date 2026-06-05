# Operator Guide — hauler-airgap-updater

> Step-by-step procedure for building and delivering RKE2 update bundles across
> an air gap. See DESIGN.md for architecture and rationale.

---

## Overview

The tool produces **two artifacts** from an internet-connected build box
(`hauler.ham.lan`), then carries them across the air gap to two servers that
cluster nodes already point at:

| Artifact | Where it goes | What it feeds |
|---|---|---|
| `rpms_<date>.tar.zst` | DNF repo server (`repo.ham.lan`) | `dnf install` on each node |
| `hauler_<date>.tar.zst` | Hauler server (`hauler.ham.lan`) | container image pulls + helm charts |

Node repo config does **not** change — nodes already point at both servers.

---

## Prerequisites

### Internet-connected build box (`hauler.ham.lan`)

| Tool | Notes |
|---|---|
| `python3` | stdlib only, no pip install |
| `hauler` v1.4.3 | `/usr/local/bin/hauler` |
| `helm` | chart image resolution |
| `kubectl` + kubeconfig | `discover` subcommand |
| `tar` | present by default |
| `zstd` | needed for RPM tar (`tar -I zstd`) |

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

If `state.json` already exists (has prior data), a fresh copy is written to
`state.discovered.json` instead — diff and merge by hand:

```bash
diff state.json state.discovered.json
# edit state.json as needed, then:
rm state.discovered.json
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
| `--no-helm-images` | Skip `helm template` image resolution (faster; charts still staged) |

---

## Step 3 — Full build (downloads + hauler store)

```bash
python3 airgap_update.py build --target 1.34 --deps-latest --no-input
```

This will:
1. Download all RPMs into `_build/rpms/el8/` and `_build/rpms/el9/`
2. Create `rpms_<date>.tar.zst` (top-level `el8/` + `el9/` dirs)
3. Run `hauler store sync` to pull images + charts (~75 images, takes 10–30 min)
4. Run `hauler store save` to create `hauler_<date>.tar.zst`
5. Update `state.json` with last-build metadata

Disk requirements: allow ~5 GB for the hauler store, ~500 MB for RPMs.

---

## Step 4 — Validate on the internet-connected cluster (do this before carrying across the gap)

Load the artifacts into hauler and actually upgrade `rocky9-rke2.ham.lan` first.
This confirms the upgrade path works end to end before media crosses the air gap.

### 4a — Ingest on hauler.ham.lan (as root)

```bash
sudo python3 airgap_update.py ingest hauler_<date>.tar.zst
```

This loads the bundle into the serving store and (re)starts:
- `hauler@registry` — OCI registry at `:5000` (images + helm charts)
- `hauler@fileserver` — HTTP fileserver at `:8080` (files; empty for standard builds)

### 4b — Extract RPMs on `repo.ham.lan`

Copy `rpms_<date>.tar.zst` to the dnf repo server, then:

```bash
# adjust path to your releasever-split repo base
tar -I zstd -xf rpms_<date>.tar.zst -C /path/to/repo/base/
# el8/ and el9/ dirs are extracted at the top level
createrepo --update /path/to/repo/base/el8/
createrepo --update /path/to/repo/base/el9/
```

### 4c — Upgrade the reference cluster (version-pinned, ordered)

> The repo now holds **multiple minors**. Do **not** use `dnf update` — it will
> skip intermediates and violate the Kubernetes no-skip rule. Pin each step.

For each minor in the upgrade path (e.g. first `1.33.12`, then `1.34.8`):

**On each control-plane server, one at a time:**

```bash
# substitute <ver> = 1.33.12.rke2r2  and  <sel> = 0.21-1
dnf install -y rke2-server-<ver>-0.el9 rke2-common-<ver>-0.el9 rke2-selinux-<sel>.el9
systemctl restart rke2-server

# wait until this node is Ready:
kubectl get nodes
# and etcd is healthy:
kubectl -n kube-system exec -it etcd-<node> -- etcdctl endpoint health \
  --cacert /var/lib/rancher/rke2/server/tls/etcd/server-ca.crt \
  --cert   /var/lib/rancher/rke2/server/tls/etcd/server-client.crt \
  --key    /var/lib/rancher/rke2/server/tls/etcd/server-client.key
```

**Then, on each agent node, one at a time:**

```bash
dnf install -y rke2-agent-<ver>-0.el9 rke2-common-<ver>-0.el9 rke2-selinux-<sel>.el9
systemctl restart rke2-agent
# wait for Ready before moving to the next agent
```

Repeat for each minor in the path before moving to the next minor.

---

## Step 5 — Carry artifacts across the air gap

Once the internet-side upgrade validates (node `Ready`, workloads healthy):

```
rpms_<date>.tar.zst   →  DNF repo server (releasever-split repo base)
hauler_<date>.tar.zst →  Hauler server
```

Both files go on removable media in the same direction (one way: in).

---

## Step 6 — Ingest on the air-gapped hauler server

```bash
# on the air-gapped hauler.ham.lan, as root:
sudo python3 airgap_update.py ingest hauler_<date>.tar.zst
```

Then extract RPMs on the air-gapped `repo.ham.lan` as in step 4b.

---

## Step 7 — Apply the upgrade on the air-gapped cluster

Follow the same ordered, version-pinned procedure as step 4c. The versions in
the upgrade path are printed by `ingest` and recorded in `state.json:last_build`.

---

## Helm-deployed dependencies (longhorn, cert-manager)

The hauler bundle **stages** these charts and images — it does **not**
automatically upgrade them. After the RKE2 upgrade is complete:

```bash
# longhorn example:
helm upgrade longhorn longhorn/longhorn \
  --namespace longhorn-system \
  --version 1.12.0 \
  --repo http://hauler.ham.lan:5000   # or OCI URL from hauler registry
```

Exact syntax depends on how your cluster is configured to reach the hauler registry.

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
| `last_build.targets` | `build` | Target versions (rke2 + helm deps) |

---

## Troubleshooting

**`helm template` hangs during build**
: It's downloading the chart index on first run. It has a 120 s timeout and will
  continue. Use `--no-helm-images` to skip image resolution and `--dry-run` to
  iterate quickly on the plan without full downloads.

**`hauler store sync` is slow**
: Normal — it's pulling ~75 images from public registries. Allow 10–30 min
  depending on connectivity. Run on the build box, not your laptop.

**`ingest` fails with "must run as root"**
: `ingest` writes the systemd unit and restarts services. Run as root or via
  `sudo`.

**`store load` fails with "no such file"**
: Pass the bundle path explicitly; `ingest` expects a positional argument with
  the `.tar.zst` filename.

**Nodes not pulling images after ingest**
: Confirm `hauler@registry` is running (`systemctl status hauler@registry`) and
  that `/etc/rancher/rke2/registries.yaml` on the nodes points to
  `hauler.ham.lan:5000`.

**`dnf install` version not found**
: The RPM bundle may not have been extracted to `repo.ham.lan` yet, or
  `createrepo` hasn't run. Verify with `dnf repoquery rke2-server`.
