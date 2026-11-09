"""Load Kubernetes manifests and build the workload -> ConfigMap/Secret reference graph."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable

import yaml

HASH_ANNOTATION = "confroll.dev/config-hash"
CONFIG_KINDS = ("ConfigMap", "Secret")
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController",
                  "Job", "CronJob", "Pod", "Rollout")
# kustomize configMapGenerator/secretGenerator suffix: 10 chars of an encoded hash (no vowels, no 0/1/3)
KUSTOMIZE_SUFFIX = re.compile(r"-[24-9bcdfghkmt]{10}$")
CHECKSUM_KEY = re.compile(r"(checksum|config-?hash|confighash|secret-?hash)", re.I)
MANIFEST_EXT = (".yaml", ".yml", ".json")

# How a container consumes a ConfigMap/Secret, and whether a running pod ever sees an edit.
RESTART_MODES = ("env", "envFrom", "subPath")  # values frozen at container start
LIVE_MODES = ("volume",)  # kubelet refreshes files (~1 min); the app must re-read them


@dataclass
class Doc:
    obj: dict
    source: str

    @property
    def kind(self) -> str:
        return self.obj.get("kind", "")

    @property
    def name(self) -> str:
        return (self.obj.get("metadata") or {}).get("name", "")

    def namespace(self, default: str = "default") -> str:
        return (self.obj.get("metadata") or {}).get("namespace") or default

    @property
    def annotations(self) -> dict:
        return (self.obj.get("metadata") or {}).get("annotations") or {}


@dataclass
class Ref:
    kind: str  # ConfigMap | Secret
    name: str
    mode: str  # env | envFrom | subPath | volume
    container: str
    key: str = ""
    optional: bool = False


@dataclass
class Workload:
    doc: Doc
    namespace: str
    refs: list = field(default_factory=list)

    @property
    def kind(self):
        return self.doc.kind

    @property
    def name(self):
        return self.doc.name

    @property
    def id(self):
        return f"{self.kind}/{self.name}" + (f" -n {self.namespace}" if self.namespace != "default" else "")

    def template(self) -> dict:
        return pod_template(self.doc.obj) or {}

    def template_annotations(self) -> dict:
        return ((self.template().get("metadata") or {}).get("annotations")) or {}

    def pod_spec(self) -> dict:
        return self.template().get("spec") or {}


def pod_template(obj: dict):
    kind = obj.get("kind")
    spec = obj.get("spec") or {}
    if kind == "Pod":
        return {"metadata": obj.get("metadata") or {}, "spec": spec}
    if kind == "CronJob":
        return ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")
    if kind in WORKLOAD_KINDS:
        return spec.get("template")
    return None


# ---------------------------------------------------------------- loading
def _iter_files(path: str):
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                if f.endswith(MANIFEST_EXT):
                    yield os.path.join(root, f)
    else:
        yield path


def parse_text(text: str, source: str) -> list:
    docs = []
    try:
        loaded = list(yaml.safe_load_all(text))
    except yaml.YAMLError as e:
        # Helm/kustomize templates (with {{ }}) are not YAML until rendered; skip them loudly.
        print(f"confroll: skipping {source}: not valid YAML ({str(e).splitlines()[0]})", file=sys.stderr)
        return docs
    for obj in loaded:
        docs.extend(_expand(obj, source))
    return docs


def _expand(obj, source):
    if not isinstance(obj, dict) or "kind" not in obj:
        return []
    if obj["kind"].endswith("List") and isinstance(obj.get("items"), list):
        out = []
        for it in obj["items"]:
            out.extend(_expand(it, source))
        return out
    return [Doc(obj, source)]


def load(paths: Iterable[str]) -> list:
    docs = []
    for p in paths:
        if p == "-":
            docs.extend(parse_text(sys.stdin.read(), "<stdin>"))
            continue
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        for f in _iter_files(p):
            with open(f, encoding="utf-8") as fh:
                docs.extend(parse_text(fh.read(), f))
    return docs


def load_git(rev: str, paths: Iterable[str], cwd: str = ".") -> list:
    """Load manifests as they were at a git revision (for `plan --git-base`)."""
    docs = []
    for p in paths:
        try:
            out = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev, "--", p], cwd=cwd,
                                 capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git ls-tree {rev} failed: {e.stderr.strip()}") from e
        for f in out.splitlines():
            if not f.endswith(MANIFEST_EXT):
                continue
            text = subprocess.run(["git", "show", f"{rev}:{f}"], cwd=cwd, capture_output=True,
                                  text=True, check=True).stdout
            docs.extend(parse_text(text, f"{rev}:{f}"))
    return docs


# ---------------------------------------------------------------- graph
def extract_refs(pod_spec: dict) -> list:
    refs = []
    containers = (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or [])
    volumes = {}
    for v in pod_spec.get("volumes") or []:
        srcs = []
        if v.get("configMap"):
            srcs.append(("ConfigMap", v["configMap"].get("name", ""), bool(v["configMap"].get("optional"))))
        if v.get("secret"):
            srcs.append(("Secret", v["secret"].get("secretName", ""), bool(v["secret"].get("optional"))))
        for s in (v.get("projected") or {}).get("sources") or []:
            if s.get("configMap"):
                srcs.append(("ConfigMap", s["configMap"].get("name", ""), bool(s["configMap"].get("optional"))))
            if s.get("secret"):
                srcs.append(("Secret", s["secret"].get("name", ""), bool(s["secret"].get("optional"))))
        if srcs:
            volumes[v.get("name")] = srcs
    for c in containers:
        cname = c.get("name", "")
        for e in c.get("env") or []:
            vf = e.get("valueFrom") or {}
            for key, kind in (("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret")):
                r = vf.get(key)
                if r:
                    refs.append(Ref(kind, r.get("name", ""), "env", cname, r.get("key", ""), bool(r.get("optional"))))
        for e in c.get("envFrom") or []:
            for key, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
                r = e.get(key)
                if r:
                    refs.append(Ref(kind, r.get("name", ""), "envFrom", cname, "", bool(r.get("optional"))))
        for m in c.get("volumeMounts") or []:
            for kind, name, opt in volumes.get(m.get("name"), []):
                sub = m.get("subPath") or m.get("subPathExpr")
                refs.append(Ref(kind, name, "subPath" if sub else "volume", cname, sub or "", opt))
    return [r for r in refs if r.name]


@dataclass
class Graph:
    docs: list
    configs: dict  # (kind, ns, name) -> Doc
    workloads: list

    def config(self, kind, ns, name):
        return self.configs.get((kind, ns, name))

    def consumers(self, kind, ns, name):
        out = []
        for w in self.workloads:
            if w.namespace != ns:
                continue
            rs = [r for r in w.refs if r.kind == kind and r.name == name]
            if rs:
                out.append((w, rs))
        return out


def build(docs: list, default_ns: str = "default") -> Graph:
    configs, workloads = {}, []
    for d in docs:
        if d.kind in CONFIG_KINDS:
            configs[(d.kind, d.namespace(default_ns), d.name)] = d
    for d in docs:
        if d.kind not in WORKLOAD_KINDS or pod_template(d.obj) is None:
            continue
        if d.kind in ("Pod", "ReplicaSet") and (d.obj.get("metadata") or {}).get("ownerReferences"):
            continue  # owned by a controller we report on instead
        w = Workload(d, d.namespace(default_ns))
        w.refs = extract_refs(w.pod_spec())
        workloads.append(w)
    return Graph(docs, configs, workloads)


# ---------------------------------------------------------------- hashing
def config_payload(doc: Doc) -> dict:
    o = doc.obj
    return {k: o.get(k) or {} for k in ("data", "binaryData", "stringData")}


def config_digest(doc: Doc) -> str:
    blob = json.dumps(config_payload(doc), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def changed_keys(old: Doc, new: Doc) -> list:
    a, b = config_payload(old), config_payload(new)
    keys = set()
    for section in a.keys() | b.keys():
        sa, sb = a.get(section) or {}, b.get(section) or {}
        for k in sa.keys() | sb.keys():
            if sa.get(k) != sb.get(k):
                keys.add(k)
    return sorted(keys)


def workload_config_hash(g: Graph, w: Workload) -> str:
    """Hash of every ConfigMap/Secret the workload reads that is defined in the manifests."""
    h = hashlib.sha256()
    seen = set()
    for r in sorted(w.refs, key=lambda r: (r.kind, r.name)):
        if (r.kind, r.name) in seen:
            continue
        seen.add((r.kind, r.name))
        d = g.config(r.kind, w.namespace, r.name)
        if d is not None:
            h.update(f"{r.kind}/{r.name}:{config_digest(d)}\n".encode())
    return h.hexdigest()[:16]
