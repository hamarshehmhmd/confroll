"""Rules: will a ConfigMap/Secret edit reach the running pods, and is the rollout safe?"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from dataclasses import asdict, dataclass

from .model import (HASH_ANNOTATION, CHECKSUM_KEY, KUSTOMIZE_SUFFIX, RESTART_MODES, Doc, Graph,
                    Workload, build, changed_keys, config_digest, workload_config_hash)

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}

RULES = {
    "CR001": "config change will not reach running pods (no rollout trigger)",
    "CR002": "only a mounted volume: files refresh, but only if the app re-reads them",
    "CR003": "rollout strategy never replaces pods on template change (OnDelete)",
    "CR004": "config-triggered rollout can take every pod down at once",
    "CR005": "confroll hash annotation is out of date",
    "CR006": "referenced ConfigMap/Secret is not in these manifests",
    "CR007": "bare Pod: nothing will ever restart it for a config change",
}


@dataclass
class Finding:
    rule: str
    severity: str
    workload: str
    message: str
    source: str
    config: str = ""

    def to_dict(self):
        d = asdict(self)
        d["title"] = RULES[self.rule]
        return d


# ---------------------------------------------------------------- rollout triggers
def _split(v):
    return {s.strip() for s in str(v or "").split(",") if s.strip()}


def trigger_for(g: Graph, w: Workload, kind: str, name: str):
    """Return a short description of what restarts pods when this ConfigMap/Secret changes, or None."""
    ann = w.doc.annotations
    low = {k.lower(): str(v).lower() for k, v in ann.items()}
    # Stakater Reloader
    if low.get("reloader.stakater.com/auto") == "true":
        return "Reloader (auto)"
    typed = "configmap" if kind == "ConfigMap" else "secret"
    if low.get(f"{typed}.reloader.stakater.com/auto") == "true":
        return f"Reloader ({typed}.auto)"
    if name in _split(ann.get(f"{typed}.reloader.stakater.com/reload")):
        return "Reloader (reload list)"
    if low.get("reloader.stakater.com/search") == "true":
        cfg = g.config(kind, w.namespace, name)
        if cfg is not None and str(cfg.annotations.get("reloader.stakater.com/match", "")).lower() == "true":
            return "Reloader (search/match)"
    # Wave (wave-k8s/wave, formerly pusher/wave)
    if low.get("wave.pusher.com/update-on-config-change") == "true":
        return "Wave"
    # immutable ConfigMap/Secret: an edit must be a new name, which changes the pod template
    cfg = g.config(kind, w.namespace, name)
    if cfg is not None and cfg.obj.get("immutable") is True:
        return "immutable (new name per change)"
    # kustomize generator hash suffix
    if KUSTOMIZE_SUFFIX.search(name):
        return "kustomize name hash"
    tann = w.template_annotations()
    if HASH_ANNOTATION in tann:
        return "confroll hash annotation"
    for k in tann:
        if CHECKSUM_KEY.search(k):
            return f"checksum annotation ({k})"
    return None


def on_delete(w: Workload) -> bool:
    spec = w.doc.obj.get("spec") or {}
    return w.kind in ("StatefulSet", "DaemonSet") and (spec.get("updateStrategy") or {}).get("type") == "OnDelete"


def unsafe_rollout_reasons(w: Workload) -> list:
    spec = w.doc.obj.get("spec") or {}
    reasons = []
    containers = w.pod_spec().get("containers") or []
    if containers and not any(c.get("readinessProbe") for c in containers):
        reasons.append("no readinessProbe, so a pod that crashes or misbehaves on the new config still counts as ready")
    if w.kind == "Deployment":
        strat = spec.get("strategy") or {}
        if strat.get("type") == "Recreate":
            reasons.append("strategy Recreate stops every old pod before a new one starts")
        mu = str((strat.get("rollingUpdate") or {}).get("maxUnavailable", ""))
        if mu in ("100%",) or (mu.isdigit() and int(mu) >= int(spec.get("replicas", 1) or 1) > 1):
            reasons.append(f"maxUnavailable {mu} lets the whole Deployment go down at once")
    return reasons


# ---------------------------------------------------------------- scan
def _cfg_id(kind, name):
    return f"{kind}/{name}"


def scan(g: Graph) -> list:
    out = []
    for w in g.workloads:
        if not w.refs:
            continue
        src = w.doc.source
        by_cfg = {}
        for r in w.refs:
            by_cfg.setdefault((r.kind, r.name), []).append(r)
        if w.kind == "Pod":
            out.append(Finding("CR007", "warning", w.id, "a bare Pod reads " + ", ".join(
                _cfg_id(*k) for k in by_cfg) + "; it keeps the old values until you delete and recreate it", src))
            continue
        batch = w.kind in ("Job", "CronJob")
        triggered_any = False
        for (kind, name), refs in by_cfg.items():
            cfg = g.config(kind, w.namespace, name)
            if cfg is None and not all(r.optional for r in refs):
                out.append(Finding("CR006", "info", w.id, f"{_cfg_id(kind, name)} is not defined in the scanned "
                                   "manifests (created elsewhere?) so confroll can't hash or diff it", src,
                                   _cfg_id(kind, name)))
            if batch:
                continue  # every new Job pod reads current config; nothing to roll
            trig = trigger_for(g, w, kind, name)
            restart = sorted({r.mode for r in refs if r.mode in RESTART_MODES})
            if trig:
                triggered_any = True
                continue
            how = ", ".join(_describe(r) for r in refs)
            if restart:
                out.append(Finding("CR001", "error", w.id,
                                   f"reads {_cfg_id(kind, name)} via {how}; editing it leaves running pods on the old "
                                   "values until something restarts them. Add `confroll stamp`, a checksum annotation, "
                                   "a kustomize generator, or Reloader", src, _cfg_id(kind, name)))
            else:
                out.append(Finding("CR002", "info", w.id,
                                   f"mounts {_cfg_id(kind, name)} as a volume ({how}); kubelet refreshes the files "
                                   "within about a minute, but nothing restarts the pods", src, _cfg_id(kind, name)))
        if batch:
            continue
        if triggered_any and on_delete(w):
            out.append(Finding("CR003", "warning", w.id, "updateStrategy is OnDelete, so the rollout trigger changes "
                               "the template but no pod is replaced until you delete it", src))
        if triggered_any:
            for reason in unsafe_rollout_reasons(w):
                out.append(Finding("CR004", "warning", w.id, "a config edit rolls this workload, but " + reason, src))
        have = w.template_annotations().get(HASH_ANNOTATION)
        if have is not None:
            want = workload_config_hash(g, w)
            if str(have) != want:
                out.append(Finding("CR005", "error", w.id, f"{HASH_ANNOTATION} is {have} but the referenced config "
                                   f"now hashes to {want}; re-run `confroll stamp`", src))
    return out


def _describe(r):
    if r.mode == "env":
        return f"env key {r.key} ({r.container})"
    if r.mode == "envFrom":
        return f"envFrom ({r.container})"
    if r.mode == "subPath":
        return f"subPath {r.key} ({r.container}, never refreshed)"
    return f"volume ({r.container})"


# ---------------------------------------------------------------- plan (diff two manifest sets)
@dataclass
class PlanItem:
    config: str
    namespace: str
    change: str  # modified | deleted
    keys: list
    workload: str
    outcome: str  # rolls | rolls-but-unsafe | live-reload-only | STALE | held (OnDelete) | next-run
    detail: str

    def to_dict(self):
        return asdict(self)


def _template_fingerprint(w: Workload) -> str:
    return json.dumps(w.template(), sort_keys=True, default=str)


def plan(base: Graph, head: Graph) -> list:
    items = []
    base_w = {(w.kind, w.namespace, w.name): w for w in base.workloads}
    for key, old in base.configs.items():
        kind, ns, name = key
        new = head.configs.get(key)
        if new is not None and config_digest(old) == config_digest(new):
            continue
        change = "deleted" if new is None else "modified"
        keys = [] if new is None else changed_keys(old, new)
        for w, refs in head.consumers(kind, ns, name):
            prev = base_w.get((w.kind, w.namespace, w.name))
            template_changed = prev is None or _template_fingerprint(prev) != _template_fingerprint(w)
            ctrl = trigger_for(head, w, kind, name)
            controller_trigger = ctrl and (ctrl.startswith("Reloader") or ctrl == "Wave")
            restart = any(r.mode in RESTART_MODES for r in refs)
            if w.kind in ("Job", "CronJob"):
                outcome, detail = "next-run", "new Job pods read the new values"
            elif w.kind == "Pod":
                outcome, detail = "STALE", "bare Pod; delete and recreate it"
            elif change == "deleted":
                outcome, detail = "BREAKS", f"{kind}/{name} is deleted but still referenced; new pods will fail to start"
            elif (template_changed or controller_trigger) and on_delete(w):
                outcome, detail = "held", "template changes but updateStrategy OnDelete replaces no pods"
            elif template_changed or controller_trigger:
                why = ctrl if controller_trigger else "pod template changed in this diff"
                reasons = unsafe_rollout_reasons(w)
                outcome = "rolls-unsafe" if reasons else "rolls"
                detail = why + ("; " + "; ".join(reasons) if reasons else "")
            elif restart:
                outcome = "STALE"
                detail = "reads it via " + ", ".join(sorted({_describe(r) for r in refs if r.mode in RESTART_MODES})) + \
                         "; pod template unchanged, so pods keep the old values"
            else:
                outcome, detail = "live-reload-only", "volume files refresh in ~1 min; only works if the app re-reads them"
            items.append(PlanItem(f"{kind}/{name}", ns, change, keys, w.id, outcome, detail))
    return items


# ---------------------------------------------------------------- stamp
def stamp(g: Graph, restart_only: bool = False) -> tuple:
    """Return (docs, changes): docs with confroll.dev/config-hash set on every pod template that reads config.

    Jobs (immutable templates) and CronJobs (each run reads fresh config) and bare Pods are left alone.
    With restart_only, workloads that only mount config as live-refreshed volumes are left alone too."""
    changes = []
    by_obj = {id(w.doc.obj): w for w in g.workloads}
    out = []
    for d in g.docs:
        w = by_obj.get(id(d.obj))
        defined = [r for r in w.refs if g.config(r.kind, w.namespace, r.name)] if w else []
        if restart_only:
            defined = [r for r in defined if r.mode in RESTART_MODES]
        if w is None or not defined or w.kind in ("Pod", "Job", "CronJob"):
            out.append(d.obj)
            continue
        obj = copy.deepcopy(d.obj)
        tmpl = _template_of(obj)
        meta = tmpl.setdefault("metadata", {}) or {}
        tmpl["metadata"] = meta
        anns = meta.get("annotations") or {}
        meta["annotations"] = anns
        new = workload_config_hash(g, w)
        if anns.get(HASH_ANNOTATION) != new:
            changes.append((w.id, anns.get(HASH_ANNOTATION), new))
        anns[HASH_ANNOTATION] = new
        out.append(obj)
    return out, changes


def _template_of(obj):
    spec = obj["spec"]
    if obj["kind"] == "CronJob":
        return spec["jobTemplate"]["spec"]["template"]
    return spec["template"]


# ---------------------------------------------------------------- stale (live cluster dump)
def _ts(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def last_modified(doc: Doc):
    """Latest write time of the object's data, from managedFields (needs --show-managed-fields)."""
    meta = doc.obj.get("metadata") or {}
    times = []
    for mf in meta.get("managedFields") or []:
        fields = json.dumps(mf.get("fieldsV1") or {})
        if mf.get("time") and (not mf.get("fieldsV1") or re.search(r'"f:(data|binaryData|stringData)"', fields)):
            times.append(_ts(mf["time"]))
    if times:
        return max(times), "managedFields"
    return _ts(meta.get("creationTimestamp")), "creationTimestamp"


def _owner(pod: Doc):
    refs = (pod.obj.get("metadata") or {}).get("ownerReferences") or []
    if not refs:
        return "Pod/" + pod.name
    o = refs[0]
    name = o.get("name", "")
    labels = (pod.obj.get("metadata") or {}).get("labels") or {}
    pth = labels.get("pod-template-hash")
    if o.get("kind") == "ReplicaSet" and pth and name.endswith("-" + pth):
        return "Deployment/" + name[: -len(pth) - 1]
    return f"{o.get('kind')}/{name}"


def pod_start(pod: Doc):
    st = pod.obj.get("status") or {}
    starts = [_ts(((cs.get("state") or {}).get("running") or {}).get("startedAt"))
              for cs in st.get("containerStatuses") or []]
    starts = [s for s in starts if s]
    if starts:
        return min(starts)
    return _ts(st.get("startTime"))


def stale(docs: list, default_ns: str = "default", include_volume: bool = False):
    """Pods whose ConfigMap/Secret was written after the pod's containers started."""
    from .model import extract_refs
    g = build([d for d in docs if d.kind != "Pod"], default_ns)
    rows, warn_no_mf = [], False
    for d in docs:
        if d.kind != "Pod" or (d.obj.get("status") or {}).get("phase") not in (None, "Running"):
            continue
        if (d.obj.get("metadata") or {}).get("deletionTimestamp"):
            continue  # already terminating
        started = pod_start(d)
        if started is None:
            continue
        ns = d.namespace(default_ns)
        seen = set()
        for r in extract_refs(d.obj.get("spec") or {}):
            if (r.kind, r.name) in seen or (r.mode not in RESTART_MODES and not include_volume):
                continue
            cfg = g.config(r.kind, ns, r.name)
            if cfg is None:
                continue
            seen.add((r.kind, r.name))
            mod, how = last_modified(cfg)
            if how != "managedFields":
                warn_no_mf = True
            if mod and mod > started:
                modes = sorted({x.mode for x in extract_refs(d.obj.get("spec") or {})
                                if x.kind == r.kind and x.name == r.name})
                rows.append({"pod": d.name, "namespace": ns, "owner": _owner(d), "config": f"{r.kind}/{r.name}",
                             "via": modes, "pod_started": started.isoformat(), "config_changed": mod.isoformat(),
                             "behind_seconds": int((mod - started).total_seconds())})
    return rows, warn_no_mf


def worst(findings) -> str:
    if not findings:
        return "none"
    return max((f.severity for f in findings), key=SEVERITY_ORDER.get)
