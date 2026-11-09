from confroll.model import build, changed_keys, config_digest, extract_refs, load, parse_text, workload_config_hash

from conftest import CM, EX


def test_extract_all_reference_kinds():
    spec = {
        "initContainers": [{"name": "init", "envFrom": [{"secretRef": {"name": "s1"}}]}],
        "containers": [{
            "name": "c",
            "env": [{"name": "X", "valueFrom": {"configMapKeyRef": {"name": "cm1", "key": "x"}}},
                    {"name": "Y", "valueFrom": {"secretKeyRef": {"name": "s2", "key": "y", "optional": True}}},
                    {"name": "Z", "value": "plain"}],
            "envFrom": [{"configMapRef": {"name": "cm2"}}],
            "volumeMounts": [{"name": "v1", "mountPath": "/a"}, {"name": "v2", "mountPath": "/b/f", "subPath": "f"},
                             {"name": "p", "mountPath": "/p"}],
        }],
        "volumes": [{"name": "v1", "configMap": {"name": "cm3"}}, {"name": "v2", "secret": {"secretName": "s3"}},
                    {"name": "p", "projected": {"sources": [{"configMap": {"name": "cm4"}}, {"secret": {"name": "s4"}},
                                                            {"serviceAccountToken": {"path": "t"}}]}},
                    {"name": "unused", "configMap": {"name": "never-mounted"}}],
    }
    got = {(r.kind, r.name, r.mode) for r in extract_refs(spec)}
    assert got == {("Secret", "s1", "envFrom"), ("ConfigMap", "cm1", "env"), ("Secret", "s2", "env"),
                   ("ConfigMap", "cm2", "envFrom"), ("ConfigMap", "cm3", "volume"), ("Secret", "s3", "subPath"),
                   ("ConfigMap", "cm4", "volume"), ("Secret", "s4", "volume")}
    assert [r.optional for r in extract_refs(spec) if r.name == "s2"] == [True]


def test_list_kind_and_multidoc_and_non_k8s_skipped():
    docs = parse_text("""
kind: List
items:
  - {kind: ConfigMap, metadata: {name: a}}
  - {kind: Secret, metadata: {name: b}}
---
just: some values file
---
""", "x")
    assert [d.kind for d in docs] == ["ConfigMap", "Secret"]


def test_templated_yaml_is_skipped(capsys):
    assert parse_text("a: {{ .Values.x }\n  b: [", "chart/templates/x.yaml") == []
    assert "skipping chart/templates/x.yaml" in capsys.readouterr().err


def test_load_directory_and_namespaces(write):
    write("k/a.yaml", CM)
    write("k/sub/b.yml", "kind: ConfigMap\nmetadata: {name: cfg, namespace: other}\n")
    write("k/notes.txt", "ignored")
    g = build(load([str(write("k/a.yaml", CM)).rsplit("/", 1)[0]]))
    assert set(g.configs) == {("ConfigMap", "default", "cfg"), ("ConfigMap", "other", "cfg")}


def test_digest_and_changed_keys(write):
    a = parse_text("kind: Secret\nmetadata: {name: s}\nstringData: {K: v1, J: same}\n", "a")[0]
    b = parse_text("kind: Secret\nmetadata: {name: s, labels: {x: y}}\nstringData: {K: v2, J: same}\n", "b")[0]
    c = parse_text("kind: Secret\nmetadata: {name: s, labels: {z: q}}\nstringData: {K: v1, J: same}\n", "c")[0]
    assert config_digest(a) != config_digest(b)
    assert config_digest(a) == config_digest(c)  # metadata-only edits don't count
    assert changed_keys(a, b) == ["K"]


def test_owned_pods_and_replicasets_ignored():
    docs = parse_text("""
kind: Pod
metadata: {name: p, ownerReferences: [{kind: ReplicaSet, name: rs}]}
spec: {containers: [{name: c}]}
---
kind: ReplicaSet
metadata: {name: rs, ownerReferences: [{kind: Deployment, name: d}]}
spec: {template: {spec: {containers: [{name: c}]}}}
---
kind: Pod
metadata: {name: bare}
spec: {containers: [{name: c}]}
""", "x")
    assert [w.name for w in build(docs).workloads] == ["bare"]


def test_workload_hash_changes_with_config_only():
    g1 = build(load([f"{EX}/shop"]))
    g2 = build(load([f"{EX}/shop-after"]))
    h1 = {w.name: workload_config_hash(g1, w) for w in g1.workloads}
    h2 = {w.name: workload_config_hash(g2, w) for w in g2.workloads}
    assert h1["web"] == h2["web"]  # web-assets unchanged
    assert h1["api"] != h2["api"] and h1["payments"] != h2["payments"]
