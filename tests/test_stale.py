from confroll.analyze import last_modified, stale
from confroll.model import load, parse_text

from conftest import EX


def test_cluster_dump():
    rows, warn = stale(load([f"{EX}/cluster-dump.yaml"]))
    assert not warn
    got = {(r["owner"], r["pod"], r["config"]) for r in rows}
    assert got == {("Deployment/api", "api-7d9f8c6b5-x2k4p", "ConfigMap/shop-config"),
                   ("Deployment/edge", "edge-5b7c9d8f4-m3n2b", "ConfigMap/shop-config")}
    # the Secret only had a label change after the pod started: not stale
    assert all(r["config"] != "Secret/payments-api-key" for r in rows)


def test_metadata_only_managed_fields_ignored():
    d = parse_text("""
kind: ConfigMap
metadata:
  name: c
  creationTimestamp: "2026-01-01T00:00:00Z"
  managedFields:
    - {time: "2026-01-02T00:00:00Z", fieldsV1: {"f:data": {"f:A": {}}}}
    - {time: "2026-01-05T00:00:00Z", fieldsV1: {"f:metadata": {"f:labels": {}}}}
""", "x")[0]
    t, how = last_modified(d)
    assert how == "managedFields" and t.day == 2


def test_falls_back_to_creation_and_warns():
    docs = parse_text("""
kind: List
items:
  - {kind: ConfigMap, metadata: {name: c, creationTimestamp: "2026-01-03T00:00:00Z"}, data: {A: "1"}}
  - kind: Pod
    metadata: {name: p}
    spec: {containers: [{name: x, envFrom: [{configMapRef: {name: c}}]}]}
    status: {phase: Running, startTime: "2026-01-01T00:00:00Z"}
""", "x")
    rows, warn = stale(docs)
    assert warn and [r["owner"] for r in rows] == ["Pod/p"]


def test_volume_mounts_only_with_flag_and_non_running_ignored():
    docs = parse_text("""
kind: List
items:
  - kind: ConfigMap
    metadata: {name: c, managedFields: [{time: "2026-01-03T00:00:00Z", fieldsV1: {"f:data": {}}}]}
  - kind: Pod
    metadata: {name: p}
    spec:
      containers: [{name: x, volumeMounts: [{name: v, mountPath: /c}]}]
      volumes: [{name: v, configMap: {name: c}}]
    status: {phase: Running, startTime: "2026-01-01T00:00:00Z"}
  - kind: Pod
    metadata: {name: done}
    spec: {containers: [{name: x, envFrom: [{configMapRef: {name: c}}]}]}
    status: {phase: Succeeded, startTime: "2026-01-01T00:00:00Z"}
""", "x")
    assert stale(docs)[0] == []
    assert [r["pod"] for r in stale(docs, include_volume=True)[0]] == ["p"]


def test_terminating_pods_ignored():
    docs = parse_text("""
kind: List
items:
  - kind: ConfigMap
    metadata: {name: c, managedFields: [{time: "2026-01-03T00:00:00Z", fieldsV1: {"f:data": {}}}]}
  - kind: Pod
    metadata: {name: p, deletionTimestamp: "2026-01-03T00:00:01Z"}
    spec: {containers: [{name: x, envFrom: [{configMapRef: {name: c}}]}]}
    status: {phase: Running, startTime: "2026-01-01T00:00:00Z"}
""", "x")
    assert stale(docs)[0] == []
