#!/usr/bin/env python3
"""airgap_update.py — build and ingest hauler bundles for in-place RKE2 airgap updates.

Subcommands (see DESIGN.md for the full plan):
  discover  online, against a reachable reference cluster — populate state.json
  build     online hauler store — resolve versions, pull artifacts, make a bundle   [stub]
  ingest    offline hauler VM   — load bundle into serving store, refresh repo      [stub]

Standard library only. Shells out to kubectl / helm / hauler.
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


def http_get(url, verbose=False):
    if verbose:
        info(f"  GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        fatal(f"HTTP {e.code} fetching {url}")
    except urllib.error.URLError as e:
        fatal(f"network error fetching {url}: {e.reason}")


def http_get_json(url, verbose=False):
    return json.loads(http_get(url, verbose=verbose))


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
    info(f"fetching RKE2 channels from {RKE2_CHANNELS_URL}")
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
    url = rke2_images_url(tag)
    info(f"  fetching image list: {url}")
    text = http_get(url)
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
        info(f"    fetching longhorn image list: {url}")
        try:
            return [l.strip() for l in http_get(url).splitlines() if l.strip()]
        except SystemExit:
            warn(f"could not fetch longhorn images for {tag}")
            return []
    if src == "helm-template":
        require("helm")
        info(f"    helm repo add {name} {dep['repoURL']} (timeout 60s)")
        run(["helm", "repo", "add", name, dep["repoURL"], "--force-update"],
            check=False, timeout=60)
        info(f"    helm template {name}/{name} --version {chart_version} (timeout 120s)")
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
            info(f"  fetching latest release for {name} from github.com/{gh}")
            try:
                latest = http_get_json(
                    f"https://api.github.com/repos/{gh}/releases/latest"
                )["tag_name"]
                info(f"    latest: {latest}")
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
        if args.no_helm_images:
            info(f"    --no-helm-images: skipping image resolution for {dep['name']}")
        else:
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

    date = f"{datetime.date.today():%m_%d_%y}"

    # --- runbook (generated early so it's available in --dry-run too)
    nodes = state.get("cluster_current", {}).get("nodes", [])
    runbook = render_runbook(path, nodes, rke2_cfg, els, arch, config)
    runbook_file = f"runbook_{date}.txt"
    with open(runbook_file, "w") as fh:
        fh.write(runbook)
    info(f"wrote {runbook_file}")

    if args.dry_run:
        print()
        for el in els:
            print(f"  [{el}] RPM urls:")
            for u in rpm_urls[el]:
                print(f"    {u}")
        print()
        print(runbook)
        warn("--dry-run: no downloads, no hauler. Review airgap_hauler.yaml, RPM list, and runbook above.")
        return

    # --- artifact 1: RPM tar (per-EL subdirs) for the dnf repo server
    require("tar")
    require("zstd")  # tar -I zstd needs the zstd binary on the build box
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
    require("hauler")
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
        "runbook": runbook_file,
        "rke2_path": path,
        "els": els,
        "targets": {"rke2": path[-1], "helm_deps": {c["name"]: c["version"] for c in charts}},
    }
    write_json(args.state, state)
    info(f"updated {args.state}")

    repo_dirs = config.get("dnf_repo", {}).get("repo_dirs", {})

    print()
    info("=" * 60)
    info("ARTIFACT DELIVERY — run these on the target servers")
    info("=" * 60)
    print()
    info(f"1) RPMs  ->  dnf repo server ({config.get('dnf_repo', {}).get('host', 'repo server')})")
    print(f"   # copy {rpm_bundle} to the repo server, then:")
    print(f"   mkdir -p <tmpdir> && tar -I zstd -xf {rpm_bundle} -C <tmpdir>")
    for el in els:
        dest = repo_dirs.get(el, f"<{el} repo dir>")
        print(f"   cp <tmpdir>/{el}/*.rpm {dest}/")
        print(f"   createrepo_c {dest}")
        print(f"   # SELinux (if enforcing):")
        print(f"   chcon -Rt httpd_sys_content_t {dest}")
        print(f"   semanage fcontext -a -t httpd_sys_content_t '{dest}(/.*)?'  # persistent")
    print()
    warn("Do NOT use 'dnf update' — the repo holds multiple minors and dnf will skip")
    warn("intermediates. Pin each step: dnf install -y rke2-server-<ver> rke2-common-<ver>")
    print()
    info(f"2) Images + charts  ->  hauler server")
    print(f"   python3 airgap_update.py ingest {hauler_bundle}")
    print()
    info(f"3) Carry runbook alongside artifacts:")
    print(f"   {runbook_file}  (copy-paste upgrade commands, per-minor per-node)")
    print()
    print(runbook)


# ----------------------------------------------------------------------------
# runbook generation
# ----------------------------------------------------------------------------
def render_runbook(path, nodes, rke2_cfg, els, arch, config):
    """Return a copy-paste-ready upgrade runbook string.

    Generates per-minor, per-EL, per-node ordered commands. Servers are listed
    before agents; within each role, nodes appear one at a time with wait steps.
    """
    hcfg = config.get("hauler", {})
    hauler_host = hcfg.get("advertise_host", "hauler")
    reg_port = hcfg.get("registry_port", 5000)
    sel_ver = rke2_cfg.get("selinux_version", "0.21")
    sel_pkgrel = rke2_cfg.get("selinux_pkg_release", "1")

    servers = [n for n in nodes if n["role"] == "control-plane"]
    agents  = [n for n in nodes if n["role"] != "control-plane"]

    def sep(char="-", width=68): return char * width

    out = []
    out.append(sep("="))
    out.append("RKE2 UPGRADE RUNBOOK")
    out.append(f"Generated : {now_iso()}")
    out.append(f"Path      : {' -> '.join(path)}")
    out.append(f"EL targets: {', '.join(els)}")
    out.append(sep("="))
    out.append("")
    out.append("PREREQUISITES")
    out.append( "  1. Snapshot / backup all nodes before starting.")
    out.append( "  2. Verify hauler registry reachable from nodes:")
    out.append(f"       curl http://{hauler_host}:{reg_port}/v2/")
    out.append( "  3. Verify RPMs are present in the dnf repo:")
    out.append( "       dnf repoquery rke2-server")
    out.append( "  NOTE: Node names below come from state.json (reference cluster).")
    out.append( "        Substitute real hostnames for air-gapped target clusters.")
    out.append("")

    for step_i, tag in enumerate(path, 1):
        base, rke2r = parse_tag(tag)
        rpm_ver = f"{base}~{rke2r}-0"
        out.append(sep("="))
        out.append(f"MINOR STEP {step_i} of {len(path)}: upgrade to {tag}")
        out.append(sep("="))
        out.append("")

        for el in els:
            srv_pkg = f"rke2-server-{rpm_ver}.{el}.{arch}"
            agt_pkg = f"rke2-agent-{rpm_ver}.{el}.{arch}"
            com_pkg = f"rke2-common-{rpm_ver}.{el}.{arch}"
            sel_pkg = f"rke2-selinux-{sel_ver}-{sel_pkgrel}.{el}.noarch"

            out.append(f"  [{el}] Control-plane servers — one at a time:")
            out.append( "         Wait for Ready + etcd healthy before proceeding to the next.")
            out.append("")
            if servers:
                for node in servers:
                    out.append(f"    # {node['name']}")
                    out.append(f"    ssh {node['name']} sudo dnf install -y \\")
                    out.append(f"      {srv_pkg} \\")
                    out.append(f"      {com_pkg} \\")
                    out.append(f"      {sel_pkg}")
                    out.append(f"    ssh {node['name']} sudo systemctl restart rke2-server")
                    out.append(f"    kubectl wait node/{node['name']} --for=condition=Ready --timeout=300s")
                    out.append(f"    kubectl get node {node['name']} -o jsonpath='{{.status.nodeInfo.kubeletVersion}}'")
                    out.append("")
            else:
                out.append( "    # (no server nodes in state.json — substitute hostnames)")
                out.append(f"    ssh <server-node> sudo dnf install -y \\")
                out.append(f"      {srv_pkg} {com_pkg} {sel_pkg}")
                out.append( "    ssh <server-node> sudo systemctl restart rke2-server")
                out.append( "    kubectl wait node/<server-node> --for=condition=Ready --timeout=300s")
                out.append("")

            out.append(f"  [{el}] Agent nodes — one at a time (only after ALL servers are Ready):")
            out.append("")
            if agents:
                for node in agents:
                    out.append(f"    # {node['name']}")
                    out.append(f"    ssh {node['name']} sudo dnf install -y \\")
                    out.append(f"      {agt_pkg} \\")
                    out.append(f"      {com_pkg} \\")
                    out.append(f"      {sel_pkg}")
                    out.append(f"    ssh {node['name']} sudo systemctl restart rke2-agent")
                    out.append(f"    kubectl wait node/{node['name']} --for=condition=Ready --timeout=300s")
                    out.append(f"    kubectl get node {node['name']} -o jsonpath='{{.status.nodeInfo.kubeletVersion}}'")
                    out.append("")
            else:
                out.append( "    # (no agent nodes recorded — substitute hostnames if applicable)")
                out.append(f"    ssh <agent-node> sudo dnf install -y \\")
                out.append(f"      {agt_pkg} {com_pkg} {sel_pkg}")
                out.append( "    ssh <agent-node> sudo systemctl restart rke2-agent")
                out.append( "    kubectl wait node/<agent-node> --for=condition=Ready --timeout=300s")
                out.append("")

    out.append(sep("="))
    out.append("POST-UPGRADE CHECKS")
    out.append(sep("="))
    out.append("  kubectl get nodes   # all Ready at target version")
    out.append("  kubectl get pods -A # no crashlooping pods")
    out.append("")
    out.append("  Helm-deployed deps (longhorn, cert-manager) are STAGED in the")
    out.append("  hauler bundle but NOT automatically upgraded — run helm upgrade")
    out.append("  for each separately when ready.")
    out.append("")

    return "\n".join(out)


# ----------------------------------------------------------------------------
# ingest (offline hauler VM)
# ----------------------------------------------------------------------------
HAULER_UNIT_PATH = "/etc/systemd/system/hauler@.service"
HAULER_UNIT_TMPL = """# managed by airgap_update.py
[Unit]
Description=Hauler Serve %I Service

[Service]
Environment="HOME={work_dir}/"
ExecStart=/usr/local/bin/hauler store serve %i -s {store_dir}
WorkingDirectory={work_dir}/
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
"""


def detect_host(config):
    advertised = config.get("hauler", {}).get("advertise_host")
    if advertised:
        return advertised
    rc, out, _ = run(["hostname", "-I"], check=False)
    host = out.split()[0] if out.split() else None
    if not host:
        fatal("could not detect host IP; set hauler.advertise_host in config.json")
    warn(f"hauler.advertise_host not set; using detected {host}")
    return host


def ensure_hauler_unit(work_dir, store_dir):
    desired = HAULER_UNIT_TMPL.format(work_dir=work_dir, store_dir=store_dir)
    existing = ""
    if os.path.exists(HAULER_UNIT_PATH):
        with open(HAULER_UNIT_PATH) as fh:
            existing = fh.read()
    if existing != desired:
        with open(HAULER_UNIT_PATH, "w") as fh:
            fh.write(desired)
        run(["systemctl", "daemon-reload"])
        info(f"wrote {HAULER_UNIT_PATH}")


def hauler_env(store_dir, work_dir):
    env = os.environ.copy()
    env["HAULER_STORE_DIR"] = store_dir
    env["HOME"] = work_dir
    return env


def cmd_ingest(args):
    """Load the hauler bundle (images + charts) into the serving store.

    RPMs are handled separately on the dnf repo server — not here.
    """
    if os.geteuid() != 0:
        fatal("ingest must run as root (systemctl / /etc writes) on the offline hauler VM")

    config = load_json(args.config) or {}
    hcfg = config.get("hauler", {})
    work_dir = hcfg.get("work_dir", "/opt/hauler")
    store_dir = hcfg.get("store_dir", "/opt/hauler/store")
    reg_port = hcfg.get("registry_port", 5000)
    fs_port = hcfg.get("fileserver_port", 8080)

    require("hauler")
    bundle = os.path.abspath(args.bundle)
    if not os.path.exists(bundle):
        fatal(f"bundle not found: {bundle}")

    env = hauler_env(store_dir, work_dir)

    # 1) load the carried store (images + charts)
    #    hauler v1.4.3: `store load -f <haul>` (NOT positional); -s sets dest store.
    warn(f"loading {bundle} into store {store_dir} (this can take a while)...")
    run(["hauler", "store", "load", "-f", bundle, "-s", store_dir], env=env)
    info("store loaded")

    # 2) ensure systemd units, (re)start registry + fileserver (charts/files)
    ensure_hauler_unit(work_dir, store_dir)
    for svc in ("hauler@registry", "hauler@fileserver"):
        run(["systemctl", "enable", svc], check=False)
        run(["systemctl", "restart", svc])
        info(f"  {svc} restarted")

    # 3) store index (handy for operators to see what's served)
    host = detect_host(config)
    rc, out, _ = run(["hauler", "store", "info", "-s", store_dir], env=env, check=False)
    if rc == 0:
        info("store contents:")
        print(out.rstrip())

    print()
    info("ingest complete. hauler serving:")
    print(f"    registry   : http://{host}:{reg_port}  (images + OCI helm charts)")
    print(f"    fileserver : http://{host}:{fs_port}   (files; empty unless Files staged)")
    print()
    warn("RPMs are delivered separately: extract rpms_*.tar.zst on the dnf repo "
         "server (releasever-split) + createrepo_c + chcon -Rt httpd_sys_content_t.")

    # Print the runbook if one was recorded for this bundle
    state = load_json(args.state) if hasattr(args, "state") else {}
    lb = (state or {}).get("last_build", {})
    runbook_file = lb.get("runbook")
    if runbook_file and os.path.exists(runbook_file):
        print()
        info(f"upgrade runbook ({runbook_file}):")
        with open(runbook_file) as fh:
            print(fh.read())
    else:
        print()
        warn("No runbook found — re-run 'build' to generate one, or refer to OPERATOR.md.")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="airgap_update.py",
        description="Build and ingest hauler bundles for in-place RKE2 airgap updates.",
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
    p_build.add_argument("--no-helm-images", action="store_true",
                         help="skip helm-template image resolution (for debugging or charts-only builds)")
    p_build.set_defaults(func=cmd_build)

    p_ing = sub.add_parser("ingest", help="load a bundle into the serving hauler store")
    p_ing.add_argument("bundle", help="path to the .tar.zst bundle")
    p_ing.add_argument("--state", default="state.json", help="path to state.json (for runbook)")
    p_ing.set_defaults(func=cmd_ingest)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
