"""confroll command line."""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

from . import __version__
from .analyze import RULES, SEVERITY_ORDER, plan, scan, stale, stamp, worst
from .model import build, load, load_git

C = {"red": "31", "green": "32", "yellow": "33", "dim": "2", "cyan": "36", "bold": "1", "mag": "35"}
SEV_COLOR = {"error": "red", "warning": "yellow", "info": "dim"}
OUTCOME_COLOR = {"rolls": "green", "next-run": "green", "rolls-unsafe": "yellow", "live-reload-only": "cyan",
                 "held": "yellow", "STALE": "red", "BREAKS": "red"}


class Out:
    def __init__(self, mode):
        self.on = mode == "always" or (mode == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))

    def c(self, color, s):
        return f"\x1b[{C[color]}m{s}\x1b[0m" if self.on else s


def _fail(findings_or_sev, threshold):
    if threshold == "none":
        return False
    sev = findings_or_sev if isinstance(findings_or_sev, str) else worst(findings_or_sev)
    return sev != "none" and SEVERITY_ORDER[sev] >= SEVERITY_ORDER[threshold]


def _load(paths, ns):
    try:
        return build(load(paths), ns)
    except FileNotFoundError as e:
        sys.exit(f"confroll: no such file or directory: {e}")


def sarif(findings):
    rules = [{"id": k, "name": k, "shortDescription": {"text": v},
              "helpUri": f"https://github.com/hamarshehmhmd/confroll#{k.lower()}"} for k, v in RULES.items()]
    level = {"error": "error", "warning": "warning", "info": "note"}
    results = [{"ruleId": f.rule, "level": level[f.severity],
                "message": {"text": f"{f.workload}: {f.message}"},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": f.source}}}]} for f in findings]
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "confroll", "version": __version__,
                                          "informationUri": "https://github.com/hamarshehmhmd/confroll",
                                          "rules": rules}}, "results": results}]}


def cmd_scan(a):
    g = _load(a.paths, a.namespace)
    findings = [f for f in scan(g) if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[a.min_severity]]
    if a.format == "json":
        print(json.dumps({"workloads": len(g.workloads), "configs": len(g.configs),
                          "findings": [f.to_dict() for f in findings]}, indent=2))
    elif a.format == "sarif":
        print(json.dumps(sarif(findings), indent=2))
    elif a.format == "github":
        lvl = {"error": "error", "warning": "warning", "info": "notice"}
        for f in findings:
            print(f"::{lvl[f.severity]} file={f.source},title=confroll {f.rule}::{f.workload}: {f.message}")
    else:
        o = Out(a.color)
        print(o.c("dim", f"{len(g.workloads)} workloads, {len(g.configs)} ConfigMaps/Secrets scanned"))
        for f in findings:
            sev = o.c(SEV_COLOR[f.severity], f"{f.severity.upper():7}")
            print(f"{sev} {o.c('bold', f.rule)} {o.c('cyan', f.workload)}  {o.c('dim', f.source)}")
            print(f"        {f.message}")
        n = {s: sum(1 for f in findings if f.severity == s) for s in ("error", "warning", "info")}
        print(o.c("bold", f"{n['error']} error(s), {n['warning']} warning(s), {n['info']} info"))
    return 1 if _fail(findings, a.fail_on) else 0


def cmd_plan(a):
    if a.git_base:
        try:
            base = build(load_git(a.git_base, a.paths), a.namespace)
        except RuntimeError as e:
            sys.exit(f"confroll: {e}")
        head = _load(a.paths, a.namespace)
    else:
        if not a.base or not a.head:
            sys.exit("confroll plan: give --base and --head, or --git-base REV with paths")
        base, head = _load(a.base, a.namespace), _load(a.head, a.namespace)
    items = plan(base, head)
    bad = [i for i in items if i.outcome in ("STALE", "BREAKS", "held")]
    risky = [i for i in items if i.outcome == "rolls-unsafe"]
    if a.format == "json":
        print(json.dumps({"items": [i.to_dict() for i in items]}, indent=2))
    elif a.format == "markdown":
        print("### confroll plan\n")
        if not items:
            print("No ConfigMap/Secret changes reach a workload in this diff.")
        else:
            print("| Config | Keys changed | Workload | Outcome | Why |\n|---|---|---|---|---|")
            for i in items:
                print(f"| `{i.config}` | {', '.join(i.keys) or '—'} | `{i.workload}` | **{i.outcome}** | {i.detail} |")
    else:
        o = Out(a.color)
        if not items:
            print("No ConfigMap/Secret changes reach a workload in this diff.")
        cur = None
        for i in items:
            if i.config != cur:
                cur = i.config
                keys = f" keys: {', '.join(i.keys)}" if i.keys else ""
                print(o.c("bold", f"{i.config}") + o.c("dim", f" ({i.change}{keys})"))
            print(f"  {o.c(OUTCOME_COLOR[i.outcome], f'{i.outcome:16}')} {o.c('cyan', i.workload)}")
            print(o.c("dim", f"  {'':16} {i.detail}"))
        if items:
            print(o.c("bold", f"{len(bad)} workload(s) will NOT get the change, {len(risky)} roll unsafely"))
    if a.fail_on == "none":
        return 0
    if a.fail_on == "unsafe":
        return 1 if bad or risky else 0
    return 1 if bad else 0


def cmd_stamp(a):
    g = _load(a.paths, a.namespace)
    docs, changes = stamp(g, a.restart_only)
    for wid, old, new in changes:
        print(f"confroll: {wid}: config-hash {old or '(none)'} -> {new}", file=sys.stderr)
    text = yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False)
    if a.output:
        with open(a.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def _human(sec):
    d, rem = divmod(int(sec), 86400)
    h, m = rem // 3600, rem % 3600 // 60
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m") if p)


def cmd_stale(a):
    try:
        docs = load(a.paths)
    except FileNotFoundError as e:
        sys.exit(f"confroll: no such file or directory: {e}")
    rows, warn = stale(docs, a.namespace, a.include_volume)
    if warn:
        print("confroll: some ConfigMaps/Secrets have no managedFields; re-run kubectl get with "
              "--show-managed-fields for accurate change times", file=sys.stderr)
    if a.format == "json":
        print(json.dumps({"stale": rows}, indent=2))
    else:
        o = Out(a.color)
        if not rows:
            print(o.c("green", "no running pod is older than the ConfigMaps/Secrets it reads at startup"))
        owners = {}
        for r in rows:
            owners.setdefault((r["namespace"], r["owner"]), []).append(r)
        for (ns, owner), rs in owners.items():
            print(o.c("red", "STALE ") + o.c("cyan", f"{owner}") + o.c("dim", f" -n {ns}") +
                  f"  {len({r['pod'] for r in rs})} pod(s)")
            for r in rs:
                print(f"      {r['pod']}: {r['config']} ({'/'.join(r['via'])}) changed "
                      f"{_human(r['behind_seconds'])} after the pod started")
            if owner.split("/")[0] in ("Deployment", "StatefulSet", "DaemonSet"):
                print(o.c("dim", f"      fix: kubectl rollout restart {owner.lower()} -n {ns}"))
    return 1 if rows and a.fail_on != "none" else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="confroll", description="Find Kubernetes workloads that won't pick up "
                                "ConfigMap/Secret changes, see what a config diff will really do, and fix it.")
    p.add_argument("--version", action="version", version=f"confroll {__version__}")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("-n", "--namespace", default="default", help="namespace for objects without one (default: default)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="static check of manifests (files, dirs, or - for stdin)")
    s.add_argument("paths", nargs="+")
    s.add_argument("--format", choices=["text", "json", "sarif", "github"], default="text")
    s.add_argument("--fail-on", choices=["error", "warning", "info", "none"], default="error")
    s.add_argument("--min-severity", choices=["info", "warning", "error"], default="info")
    s.set_defaults(fn=cmd_scan)

    pl = sub.add_parser("plan", help="diff two manifest sets: which workloads get a ConfigMap/Secret change")
    pl.add_argument("paths", nargs="*", help="head manifests (with --git-base)")
    pl.add_argument("--base", nargs="+")
    pl.add_argument("--head", nargs="+")
    pl.add_argument("--git-base", metavar="REV", help="compare paths at REV (e.g. origin/main) with the working tree")
    pl.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    pl.add_argument("--fail-on", choices=["stale", "unsafe", "none"], default="stale")
    pl.set_defaults(fn=cmd_plan)

    st = sub.add_parser("stamp", help="add a config-hash annotation to pod templates (stdout; works as a "
                        "kustomize/helm post-renderer)")
    st.add_argument("paths", nargs="+")
    st.add_argument("-o", "--output")
    st.add_argument("--restart-only", action="store_true",
                    help="skip workloads that only mount config as (live-refreshed) volumes")
    st.set_defaults(fn=cmd_stamp)

    sl = sub.add_parser("stale", help="find running pods older than their config, from "
                        "`kubectl get pods,cm,secrets -o yaml --show-managed-fields`")
    sl.add_argument("paths", nargs="+")
    sl.add_argument("--format", choices=["text", "json"], default="text")
    sl.add_argument("--include-volume", action="store_true", help="also report volume mounts (normally live-refreshed)")
    sl.add_argument("--fail-on", choices=["stale", "none"], default="stale")
    sl.set_defaults(fn=cmd_stale)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
