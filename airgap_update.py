#!/usr/bin/env python3
"""airgap_update.py — download artifacts + generate the apply runbook for in-place RKE2 airgap updates.

Subcommands (see DESIGN.md for the full plan):
  discover  online, against a reachable reference cluster — populate state.json
  build     online — resolve versions, download RPMs + images/charts, emit two
            tar.zst artifacts and RUNBOOK.md
  runbook   regenerate RUNBOOK.md from the last build recorded in state.json

Loading the artifacts into the hauler servers and applying the node upgrade are
MANUAL steps — the generated RUNBOOK.md spells them out. Standard library only;
shells out to kubectl / helm / hauler / tar.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------------------
# logging helpers (mirrors the original script's info/warn/fatal style)
# ----------------------------------------------------------------------------
RED = "\x1b[0;31m"
GREEN = "\x1b[32m"
BLUE = "\x1b[34m"
YELLOW = "\x1b[33m"
NC = "\x1b[0m"


def info(msg):
    print(f"{GREEN}[info]{NC} {msg}")


def warn(msg):
    print(f"{YELLOW}[warn]{NC} {msg}")


def fatal(msg):
    print(f"{RED}[error]{NC} {msg}", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------
# small utilities
# ----------------------------------------------------------------------------
def run(cmd, env=None, check=True, timeout=None):
    """Run a command, return (rc, stdout, stderr). `cmd` is a list.

    `timeout` (seconds) guards against unbounded hangs on external tools; a
    timeout is fatal when check=True, else returns rc=124 like coreutils.
    """
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if check:
            fatal(f"command timed out after {timeout}s: {' '.join(cmd)}")
        return 124, "", f"timed out after {timeout}s"
    if check and proc.returncode != 0:
        fatal(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc.returncode, proc.stdout, proc.stderr


def require(tool):
    """Fatal unless `tool` is on PATH."""
    if shutil.which(tool) is None:
        fatal(f"required tool not found on PATH: {tool}")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as fh:
        return json.load(fh)


def write_json(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# discover
# ----------------------------------------------------------------------------
CONTROL_PLANE_LABELS = (
    "node-role.kubernetes.io/control-plane",
    "node-role.kubernetes.io/master",
)


def kube_env(kubeconfig):
    env = os.environ.copy()
    if kubeconfig:
        env["KUBECONFIG"] = os.path.expanduser(kubeconfig)
    return env


def discover_rke2_version(env):
    """Return (chosen_version, [(node, role, version), ...]).

    kubeletVersion carries the rke2 suffix, e.g. 'v1.33.6+rke2r1'. During an
    in-progress upgrade nodes may differ; we choose the LOWEST control-plane
    version as the starting point (that's what we'd upgrade *from*).
    """
    _, out, _ = run(["kubectl", "get", "nodes", "-o", "json"], env=env)
    nodes = json.loads(out).get("items", [])
    if not nodes:
        fatal("kubectl returned no nodes — is KUBECONFIG pointing at the cluster?")

    rows = []
    for n in nodes:
        name = n["metadata"]["name"]
        labels = n["metadata"].get("labels", {})
        is_cp = any(lbl in labels for lbl in CONTROL_PLANE_LABELS)
        ver = n["status"]["nodeInfo"]["kubeletVersion"]
        rows.append((name, "control-plane" if is_cp else "worker", ver))

    # Plan from the LOWEST node version (across servers AND agents), not just the
    # control plane: in a multi-node cluster an agent may lag, and the bundle
    # must cover every node's full upgrade path. Over-fetching is harmless.
    all_versions = [v for (_, _, v) in rows]
    chosen = min(all_versions, key=_rke2_sort_key)

    distinct = sorted(set(all_versions), key=_rke2_sort_key)
    if len(distinct) > 1:
        warn(f"mixed node versions: {', '.join(distinct)} — planning from the "
             f"lowest ({chosen}) so all nodes are covered")
    return chosen, rows


def _rke2_sort_key(tag):
    """Sort key for tags like 'v1.33.6+rke2r1' -> (1,33,6,1). Tolerant of junk."""
    core = tag.lstrip("v")
    rke2r = 0
    if "+rke2r" in core:
        core, _, suffix = core.partition("+rke2r")
        rke2r = int("".join(ch for ch in suffix if ch.isdigit()) or 0)
    parts = core.split(".")
    nums = []
    for p in parts[:3]:
        nums.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2], rke2r)


def discover_helm_deps(env, helm_deps):
    """Map configured helm_deps to their installed release, if present.

    helm list chart field looks like 'longhorn-1.7.2'; app_version like 'v1.7.2'.
    """
    _, out, _ = run(["helm", "list", "-A", "-o", "json"], env=env)
    releases = json.loads(out)

    found = {}
    for dep in helm_deps:
        name = dep["name"]
        match = None
        for rel in releases:
            chart = rel.get("chart", "")
            # chart == '<name>-<version>'; split on the LAST hyphen-number boundary
            if chart.startswith(name + "-"):
                match = rel
                break
        if match is None:
            warn(f"helm dep '{name}' not found in any namespace on the reference cluster")
            continue
        chart_version = match["chart"][len(name) + 1 :]
        found[name] = {
            "release": match.get("name"),
            "namespace": match.get("namespace"),
            "chart_version": chart_version,
            "app_version": match.get("app_version"),
        }
        info(f"  {name}: chart {chart_version} (app {match.get('app_version')}) in ns {match.get('namespace')}")
    return found


def cmd_discover(args):
    require("kubectl")
    require("helm")
    config = load_json(args.config) or {}
    helm_deps = config.get("helm_deps", [])
    env = kube_env(args.kubeconfig)

    info("discovering RKE2 version from reference cluster")
    rke2_version, node_rows = discover_rke2_version(env)
    info(f"  RKE2 current: {rke2_version}")
    for name, role, ver in node_rows:
        print(f"    - {name} [{role}] {ver}")

    info("discovering helm-deployed dependencies")
    deps = discover_helm_deps(env, helm_deps)

    discovered = {
        "cluster_current": {
            "rke2": rke2_version,
            "helm_deps": deps,
            "nodes": [
                {"name": n, "role": r, "version": v} for (n, r, v) in node_rows
            ],
            "discovered_at": now_iso(),
        },
        "last_build": {"timestamp": None, "bundle": None, "rke2_path": [], "targets": {}},
    }

    # Never clobber a hand-edited state silently.
    if os.path.exists(args.state):
        out_path = args.state.replace(".json", "") + ".discovered.json"
        # keep the existing last_build record around for reference
        existing = load_json(args.state) or {}
        if existing.get("last_build"):
            discovered["last_build"] = existing["last_build"]
        write_json(out_path, discovered)
        warn(f"{args.state} already exists — wrote fresh discovery to {out_path}")
        warn("review the diff and merge into state.json by hand:")
        print(f"    diff {args.state} {out_path}")
    else:
        write_json(args.state, discovered)
        info(f"wrote {args.state}")
        warn("review/edit it — the reference cluster may differ from the air-gapped targets")


# ----------------------------------------------------------------------------
# network helpers
# ----------------------------------------------------------------------------
USER_AGENT = "airgap-update/0.1"
RKE2_CHANNELS_URL = "https://update.rke2.io/v1-release/channels"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        fatal(f"HTTP {e.code} fetching {url}")
    except urllib.error.URLError as e:
        fatal(f"network error fetching {url}: {e.reason}")


def http_get_json(url):
    return json.loads(http_get(url))


# ----------------------------------------------------------------------------
# interactive prompts (skipped when --no-input / overrides are supplied)
# ----------------------------------------------------------------------------
def ask(prompt, default=None, no_input=False):
    if no_input:
        return default
    suffix = f" [{default}]" if default is not None else ""
    try:
        resp = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return resp or default


# ----------------------------------------------------------------------------
# rke2 version logic
# ----------------------------------------------------------------------------
def minor_of(tag):
    """('v1.33.12+rke2r2') -> (1, 33)."""
    k = _rke2_sort_key(tag)
    return (k[0], k[1])


def fetch_rke2_minor_latest():
    """Return {(maj,min): tag} — latest patch per minor, from the rke2 channels.

    The channels endpoint exposes one channel per minor (name like 'v1.33')
    whose 'latest' is that minor's newest patch — exactly the minor-level data
    the upgrade-path planner needs.
    """
    data = http_get_json(RKE2_CHANNELS_URL).get("data", [])
    out = {}
    for ch in data:
        name = ch.get("name", "")
        latest = ch.get("latest")
        if re.fullmatch(r"v\d+\.\d+", name) and latest:
            out[minor_of(latest)] = latest
    if not out:
        fatal("could not parse any vX.Y channels from the rke2 channels endpoint")
    return out


def compute_path(current_tag, target_minor, minor_latest):
    """List of rke2 tags to bundle, walking minors current..target inclusive.

    Each hop is that minor's latest patch (so it absorbs patch drift and lands
    on the newest patch before crossing each minor boundary).
    """
    cur = minor_of(current_tag)
    if target_minor < cur:
        fatal(f"target minor {target_minor} is older than current {cur}")
    path = []
    maj = cur[0]
    for mnr in range(cur[1], target_minor[1] + 1):
        key = (maj, mnr)
        if key not in minor_latest:
            fatal(f"no rke2 channel found for minor {maj}.{mnr}")
        path.append(minor_latest[key])
    return path


def parse_tag(tag):
    """('v1.33.12+rke2r2') -> ('1.33.12', 'rke2r2')."""
    core = tag.lstrip("v")
    base, _, suffix = core.partition("+")
    return base, (suffix or "rke2r1")


# ----------------------------------------------------------------------------
# artifact URL builders (patterns mirror hauler_all_the_things.sh)
# ----------------------------------------------------------------------------
def _enc(tag):
    return tag.replace("+", "%2B")


def rke2_rpm_files(tag, el, arch):
    base, rke2r = parse_tag(tag)
    pkg_tag = f"v{base}+{rke2r}.stable.0"
    urls = []
    for comp in ("rke2-common", "rke2-server", "rke2-agent"):
        fname = f"{comp}-{base}.{rke2r}-0.{el}.{arch}.rpm"
        urls.append(
            f"https://github.com/rancher/rke2-packaging/releases/download/{_enc(pkg_tag)}/{fname}"
        )
    return urls


def rke2_selinux_file(rke2_cfg, el):
    ver = rke2_cfg["selinux_version"]
    rel = rke2_cfg["selinux_release"]
    pkgrel = rke2_cfg.get("selinux_pkg_release", "1")
    fname = f"rke2-selinux-{ver}-{pkgrel}.{el}.noarch.rpm"
    return f"https://github.com/rancher/rke2-selinux/releases/download/v{ver}.{rel}/{fname}"


def rke2_images_url(tag):
    return (
        f"https://github.com/rancher/rke2/releases/download/{_enc(tag)}"
        "/rke2-images-all.linux-amd64.txt"
    )


def helm_binary_file():
    tag = http_get_json("https://api.github.com/repos/helm/helm/releases/latest")["tag_name"]
    return f"https://get.helm.sh/helm-{tag}-linux-amd64.tar.gz"


# ----------------------------------------------------------------------------
# image gathering
# ----------------------------------------------------------------------------
def rke2_images_for(tag, exclude_patterns):
    text = http_get(rke2_images_url(tag))
    imgs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(p in line for p in exclude_patterns):
            continue
        imgs.append(line)
    return imgs


def helm_dep_images(dep, chart_version):
    """Best-effort image list for a helm dep. Warns (not fatal) on failure."""
    src = dep.get("image_source")
    name = dep["name"]
    if src == "longhorn-images-txt":
        tag = chart_version if chart_version.startswith("v") else "v" + chart_version
        url = (
            f"https://github.com/longhorn/longhorn/releases/download/{tag}"
            "/longhorn-images.txt"
        )
        try:
            return [l.strip() for l in http_get(url).splitlines() if l.strip()]
        except SystemExit:
            warn(f"could not fetch longhorn images for {tag}")
            return []
    if src == "helm-template":
        require("helm")
        run(["helm", "repo", "add", name, dep["repoURL"], "--force-update"],
            check=False, timeout=60)
        rc, out, _ = run(
            ["helm", "template", f"{name}/{name}", "--version", chart_version],
            check=False, timeout=120,
        )
        if rc != 0:
            warn(f"helm template failed/timed out for {name} {chart_version}; skipping its images")
            return []
        imgs = []
        for line in out.splitlines():
            m = re.search(r"image:\s*\"?([^\"\s]+)\"?", line)
            if m:
                imgs.append(m.group(1))
        return sorted(set(imgs))
    warn(f"unknown image_source '{src}' for dep {name}")
    return []


# ----------------------------------------------------------------------------
# manifest generation
# ----------------------------------------------------------------------------
def render_manifest(images, charts, files, platform):
    """Render the hauler manifest (images + charts [+ optional files]).

    With the two-artifact split, RPMs no longer live here — this manifest feeds
    the hauler store (images/charts) only. `files` is normally empty.
    """
    lines = []
    lines.append("apiVersion: content.hauler.cattle.io/v1")
    lines.append("kind: Images")
    lines.append("metadata:")
    lines.append("  name: rke2-airgap-images")
    lines.append("  annotations:")
    lines.append(f"    hauler.dev/platform: {platform}")
    lines.append("spec:")
    lines.append("  images:")
    for img in images:
        lines.append(f"    - name: {img}")

    if charts:
        lines.append("---")
        lines.append("apiVersion: content.hauler.cattle.io/v1")
        lines.append("kind: Charts")
        lines.append("metadata:")
        lines.append("  name: rke2-airgap-charts")
        lines.append("spec:")
        lines.append("  charts:")
        for c in charts:
            lines.append(f"    - name: {c['name']}")
            lines.append(f"      repoURL: {c['repoURL']}")
            lines.append(f"      version: {c['version']}")

    if files:
        lines.append("---")
        lines.append("apiVersion: content.hauler.cattle.io/v1")
        lines.append("kind: Files")
        lines.append("metadata:")
        lines.append("  name: rke2-airgap-files")
        lines.append("spec:")
        lines.append("  files:")
        for f in files:
            lines.append(f"    - path: {f}")

    return "\n".join(lines) + "\n"


def download_file(url, dest):
    """Stream a URL to a local file."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except urllib.error.HTTPError as e:
        fatal(f"HTTP {e.code} downloading {url}")
    except urllib.error.URLError as e:
        fatal(f"network error downloading {url}: {e.reason}")


# ----------------------------------------------------------------------------
# build
# ----------------------------------------------------------------------------
def resolve_target_minor(current_tag, minor_latest, args):
    """Interactive (or --target) selection of the target minor."""
    cur = minor_of(current_tag)
    ahead = sorted(m for m in minor_latest if m >= cur)

    if args.target:
        t = args.target.lstrip("v")
        parts = t.split(".")
        tgt = (int(parts[0]), int(parts[1]))
        if tgt not in minor_latest:
            fatal(f"--target {args.target}: no rke2 channel for minor {tgt[0]}.{tgt[1]}")
        return tgt

    print()
    info(f"current minor: {cur[0]}.{cur[1]} (latest patch {minor_latest.get(cur, '?')})")
    print("available targets:")
    for i, m in enumerate(ahead, 1):
        marker = "  (current minor — patch bump only)" if m == cur else ""
        print(f"  {i}) {m[0]}.{m[1]}  -> {minor_latest[m]}{marker}")
    choice = ask("select target number", default="1", no_input=args.no_input)
    try:
        return ahead[int(choice) - 1]
    except (ValueError, IndexError):
        fatal(f"invalid selection: {choice}")


def resolve_helm_deps(config, state, args):
    """Return list of {name, repoURL, version, chart_version_format, _dep} to stage."""
    deps_cfg = config.get("helm_deps", [])
    current = state.get("cluster_current", {}).get("helm_deps", {})
    resolved = []
    for dep in deps_cfg:
        name = dep["name"]
        gh = dep.get("github_repo")
        latest = None
        if gh:
            try:
                latest = http_get_json(
                    f"https://api.github.com/repos/{gh}/releases/latest"
                )["tag_name"]
            except SystemExit:
                warn(f"could not fetch latest release for {name}")
        cur_app = current.get(name, {}).get("app_version")
        default = latest or cur_app
        if args.deps_latest:
            chosen = latest or default
        else:
            chosen = ask(
                f"version for helm dep '{name}' (current on cluster: {cur_app or 'n/a'})",
                default=default,
                no_input=args.no_input,
            )
        if not chosen:
            warn(f"no version chosen for {name}; skipping")
            continue
        # chart version vs app/tag version
        fmt = dep.get("chart_version_format", "as-is")
        chart_version = chosen.lstrip("v") if fmt == "strip-v" else chosen
        resolved.append(
            {
                "name": name,
                "repoURL": dep["repoURL"],
                "version": chart_version,
                "_dep": dep,
            }
        )
    return resolved


def cmd_build(args):
    config = load_json(args.config) or {}
    state = load_json(args.state) or {}
    rke2_cfg = config.get("rke2", {})
    els = config.get("el_versions") or [config["el_version"]]  # list; legacy single supported
    arch = config["arch_rpm"]
    platform = config.get("platform", "linux/amd64")
    exclude = rke2_cfg.get("exclude_image_patterns", [])

    # --- current version (confirm/override; air-gap value may be hand-supplied)
    state_current = state.get("cluster_current", {}).get("rke2")
    current_tag = args.current or ask(
        "assumed current RKE2 version on the air-gapped cluster",
        default=state_current,
        no_input=args.no_input,
    )
    if not current_tag:
        fatal("no current RKE2 version (run `discover`, edit state.json, or pass --current)")
    info(f"current RKE2: {current_tag}")

    # --- resolve target + path
    minor_latest = fetch_rke2_minor_latest()
    target_minor = resolve_target_minor(current_tag, minor_latest, args)
    path = compute_path(current_tag, target_minor, minor_latest)
    info(f"upgrade path: {' -> '.join(path)}")

    # --- gather artifacts:
    #   RPMs  -> per-EL dict (separate tar.zst for the dnf repo server)
    #   images/charts -> hauler manifest (separate hauler store bundle)
    images = set()
    rpm_urls = {el: [] for el in els}  # el -> [url, ...]
    for tag in path:
        info(f"  gathering rke2 {tag}")
        for el in els:
            rpm_urls[el].extend(rke2_rpm_files(tag, el, arch))
        imgs = rke2_images_for(tag, exclude)  # images are EL-independent
        info(f"    {len(imgs)} images")
        images.update(imgs)
    for el in els:
        rpm_urls[el].append(rke2_selinux_file(rke2_cfg, el))

    # --- helm deps (staged into hauler: charts + images)
    charts = []
    for dep in resolve_helm_deps(config, state, args):
        info(f"  staging helm dep {dep['name']} chart {dep['version']}")
        charts.append({"name": dep["name"], "repoURL": dep["repoURL"], "version": dep["version"]})
        imgs = helm_dep_images(dep["_dep"], dep["version"])
        info(f"    {len(imgs)} images")
        images.update(imgs)

    # --- optional extra hauler Files (e.g. helm binary; off by default now)
    files = []
    if config.get("include_helm_binary"):
        files.append(helm_binary_file())

    rpm_count = sum(len(v) for v in rpm_urls.values())

    # --- write hauler manifest (images + charts)
    manifest = render_manifest(sorted(images), charts, files, platform)
    manifest_path = "airgap_hauler.yaml"
    with open(manifest_path, "w") as fh:
        fh.write(manifest)
    info(
        f"wrote {manifest_path}: {len(images)} images, {len(charts)} charts; "
        f"RPMs to fetch: {rpm_count} ({', '.join(f'{el}={len(rpm_urls[el])}' for el in els)})"
    )

    if args.dry_run:
        print()
        for el in els:
            print(f"  [{el}] RPM urls:")
            for u in rpm_urls[el]:
                print(f"    {u}")
        warn("--dry-run: no downloads, no hauler. Review airgap_hauler.yaml + the RPM list above.")
        return

    date = f"{datetime.date.today():%m_%d_%y}"

    # --- fail fast on missing build tools before any downloads
    require("tar")
    require("zstd")  # tar -I zstd needs the zstd binary on the build box
    require("hauler")

    # --- artifact 1: RPM tar (per-EL subdirs) for the dnf repo server
    build_dir = config.get("build_dir", "_build")
    rpms_root = os.path.join(build_dir, "rpms")
    if os.path.exists(rpms_root):
        shutil.rmtree(rpms_root)
    for el in els:
        el_dir = os.path.join(rpms_root, el)
        os.makedirs(el_dir, exist_ok=True)
        info(f"downloading {len(rpm_urls[el])} {el} RPM(s)")
        for url in rpm_urls[el]:
            dest = os.path.join(el_dir, url.rsplit("/", 1)[-1])
            download_file(url, dest)
    rpm_bundle = f"rpms_{date}.tar.zst"
    info(f"creating {rpm_bundle} (top-level dirs: {', '.join(els)})")
    run(["tar", "-I", "zstd", "-cf", os.path.abspath(rpm_bundle), "-C", rpms_root, "."])

    # --- artifact 2: hauler store (images + charts)
    #    hauler v1.4.3: sync -f <manifest> -s <store>; save -f <out> -s <store>.
    build_store = os.path.join(build_dir, "store")
    warn("hauler store sync — this will take a while...")
    run(["hauler", "store", "sync", "-f", manifest_path, "-s", build_store])
    hauler_bundle = f"hauler_{date}.tar.zst"
    info(f"saving {hauler_bundle}")
    run(["hauler", "store", "save", "-f", hauler_bundle, "-s", build_store])

    state.setdefault("last_build", {})
    state["last_build"] = {
        "timestamp": now_iso(),
        "rpm_bundle": rpm_bundle,
        "hauler_bundle": hauler_bundle,
        "rke2_path": path,
        "els": els,
        "targets": {"rke2": path[-1], "helm_deps": {c["name"]: c["version"] for c in charts}},
    }
    write_json(args.state, state)
    info(f"updated {args.state}")

    # --- generate the operator runbook (manual hauler load + node apply) ----
    repo_host = config.get("dnf_repo", {}).get("host", "the dnf repo server")
    store_dir = config.get("hauler", {}).get("store_dir", "/opt/hauler/store")
    selinux_version = rke2_cfg.get("selinux_version", "")
    write_runbook(path, els, rpm_bundle, hauler_bundle, store_dir, repo_host,
                  selinux_version, charts)

    print()
    info("two artifacts to carry across the air gap:")
    print(f"    {rpm_bundle}    -> dnf repo server ({repo_host})")
    print(f"    {hauler_bundle} -> hauler server(s)")
    info(f"follow {RUNBOOK_PATH} to load them and apply the upgrade.")


# ----------------------------------------------------------------------------
# runbook (operator apply guide, generated by build)
# ----------------------------------------------------------------------------
#
# The tool's job ends at *downloading* the two artifacts. Loading them into the
# hauler servers and applying the upgrade on the nodes are MANUAL steps — this
# runbook spells them out with concrete, copy/paste-ready, single-line commands
# derived from the exact versions and EL releases this build produced.
RUNBOOK_PATH = "RUNBOOK.md"


def _dnf_ver(tag):
    """('v1.33.12+rke2r2') -> '1.33.12.rke2r2' — the dnf version token to pin."""
    base, rke2r = parse_tag(tag)
    return f"{base}.{rke2r}"


def render_runbook(path, els, rpm_bundle, hauler_bundle, store_dir, repo_host,
                   selinux_version, charts):
    """Return the RUNBOOK.md text: manual hauler load + version-pinned node apply.

    Every command is a single line (no backslash continuations) so it survives
    copy/paste. Only the EL releases this build covers (`els`) appear.
    """
    sel = f"rke2-selinux-{selinux_version}"
    L = []
    L.append("# RKE2 air-gap upgrade runbook")
    L.append("")
    L.append(f"Generated by `airgap_update.py build`. Upgrade path: "
             f"**{' -> '.join(path)}**.")
    L.append("")
    L.append("Two artifacts came out of the build — carry both across the gap:")
    L.append("")
    L.append(f"- `{rpm_bundle}` -> dnf repo server (`{repo_host}`), EL: "
             f"{', '.join(els)}")
    L.append(f"- `{hauler_bundle}` -> hauler server(s) (images + charts)")
    L.append("")

    # --- 1. RPMs onto the dnf repo server -----------------------------------
    L.append("## 1. Publish RPMs on the dnf repo server")
    L.append("")
    dirs = " ".join(f"./{el}/" for el in els)
    L.append(f"Copy `{rpm_bundle}` to `{repo_host}` and extract it — it contains "
             f"one dir per EL ({', '.join(els)}) of `.rpm`s. Drop those into "
             "wherever your repo serves each releasever and refresh metadata "
             "(`createrepo`/`createrepo_c`) the way you already do:")
    L.append("")
    L.append("```bash")
    L.append(f"tar -xf {rpm_bundle}   # yields {dirs}")
    L.append("```")
    L.append("")

    # --- 2. Images/charts into each hauler server ---------------------------
    L.append("## 2. Load images + charts into each hauler server")
    L.append("")
    L.append("Run on the internet-connected hauler server AND again on the "
             f"air-gapped hauler server (after copying `{hauler_bundle}` to it). "
             f"`-f` points at the bundle, `-s` at the serving store:")
    L.append("")
    L.append("```bash")
    L.append(f"sudo hauler store load -f {hauler_bundle} -s {store_dir}")
    L.append("sudo systemctl restart hauler@registry hauler@fileserver")
    L.append(f"hauler store info -s {store_dir}   # verify contents")
    L.append("```")
    L.append("")

    # --- 3. Node apply, version-pinned, one minor at a time -----------------
    L.append("## 3. Apply on the nodes (one minor at a time, cluster-wide)")
    L.append("")
    L.append("> The repo now holds multiple minors. **Do NOT run a blind "
             "`dnf update`** — it would jump straight to the newest minor and "
             "skip the intermediates (Kubernetes can't skip minors). Pin every "
             "step to the exact version below.")
    L.append("")
    L.append("For each step: upgrade the **control-plane servers one at a time** "
             "(after each `restart`, wait for that node to report `Ready` and "
             "etcd to be healthy before moving to the next server), **then** the "
             "**agents one at a time**. Only start the next step once every node "
             "is on the current one.")
    L.append("")
    for i, tag in enumerate(path, 1):
        v = _dnf_ver(tag)
        L.append(f"### Step {i}: {tag}")
        L.append("")
        L.append("Servers (one at a time):")
        L.append("")
        L.append("```bash")
        L.append(f"sudo dnf install -y rke2-server-{v} rke2-common-{v} {sel}")
        L.append("sudo systemctl restart rke2-server")
        L.append("```")
        L.append("")
        L.append("Agents (one at a time, after all servers in this step):")
        L.append("")
        L.append("```bash")
        L.append(f"sudo dnf install -y rke2-agent-{v} rke2-common-{v} {sel}")
        L.append("sudo systemctl restart rke2-agent")
        L.append("```")
        L.append("")
    L.append("> Heads-up (multi-server): `systemctl restart` is the cutover for "
             "that node, but the cluster's bundled components (coredns, canal, "
             "ingress-nginx, metrics-server, ...) are reconciled cluster-wide. "
             "Until a **majority of servers** are on the new version, the lagging "
             "servers will drag those component versions back down — i.e. a "
             "half-finished step can appear to \"downgrade\" minutes later. Don't "
             "stop partway through a step.")
    L.append("")

    # --- 4. Helm deps (staged, not driven by this tool) ---------------------
    if charts:
        L.append("## 4. Helm dependencies (staged — upgrade separately)")
        L.append("")
        L.append("These charts + their images are in the hauler store, but this "
                 "tool does **not** drive their upgrade (they're not part of "
                 "RKE2's bundled set). Run `helm upgrade` against the hauler "
                 "registry on your own schedule:")
        L.append("")
        for c in charts:
            L.append(f"- `{c['name']}` -> chart `{c['version']}`")
        L.append("")
    return "\n".join(L) + "\n"


def write_runbook(path, els, rpm_bundle, hauler_bundle, store_dir, repo_host,
                  selinux_version, charts):
    text = render_runbook(path, els, rpm_bundle, hauler_bundle, store_dir,
                          repo_host, selinux_version, charts)
    with open(RUNBOOK_PATH, "w") as fh:
        fh.write(text)
    info(f"wrote {RUNBOOK_PATH} ({len(path)}-step path, EL: {', '.join(els)})")


def cmd_runbook(args):
    """Regenerate RUNBOOK.md from the last build recorded in state.json."""
    config = load_json(args.config) or {}
    state = load_json(args.state) or {}
    lb = state.get("last_build") or {}
    path = lb.get("rke2_path")
    if not path:
        fatal("no recorded build in state.json — run `build` first")
    els = lb.get("els") or config.get("el_versions") or [config.get("el_version")]
    store_dir = config.get("hauler", {}).get("store_dir", "/opt/hauler/store")
    repo_host = config.get("dnf_repo", {}).get("host", "the dnf repo server")
    selinux_version = config.get("rke2", {}).get("selinux_version", "")
    charts = [{"name": n, "version": v}
              for n, v in (lb.get("targets", {}).get("helm_deps") or {}).items()]
    write_runbook(path, els, lb.get("rpm_bundle"), lb.get("hauler_bundle"),
                  store_dir, repo_host, selinux_version, charts)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="airgap_update.py",
        description="Download artifacts and generate the apply runbook for in-place RKE2 airgap updates.",
    )
    parser.add_argument("--config", default="config.json", help="path to config.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_disc = sub.add_parser("discover", help="populate state.json from a reachable cluster")
    p_disc.add_argument("--kubeconfig", default=None, help="KUBECONFIG to use (default: env)")
    p_disc.add_argument("--state", default="state.json", help="path to state.json")
    p_disc.set_defaults(func=cmd_discover)

    p_build = sub.add_parser("build", help="resolve versions, pull artifacts, make a bundle")
    p_build.add_argument("--state", default="state.json", help="path to state.json")
    p_build.add_argument("--dry-run", action="store_true", help="write manifest, skip hauler sync/save")
    p_build.add_argument("--current", default=None, help="override assumed current RKE2 version")
    p_build.add_argument("--target", default=None, help="target minor or version, e.g. 1.34 (skip prompt)")
    p_build.add_argument("--deps-latest", action="store_true", help="take latest for all helm deps")
    p_build.add_argument("--no-input", action="store_true", help="non-interactive; use defaults")
    p_build.set_defaults(func=cmd_build)

    p_run = sub.add_parser("runbook", help="(re)generate RUNBOOK.md from the last build in state.json")
    p_run.add_argument("--state", default="state.json", help="path to state.json")
    p_run.set_defaults(func=cmd_runbook)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
