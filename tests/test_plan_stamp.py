import subprocess

import yaml

from confroll.analyze import plan, scan, stamp
from confroll.model import HASH_ANNOTATION, build, load, load_git, parse_text

from conftest import EX


def outcomes(base, head):
    return {(i.config, i.workload): i.outcome for i in plan(base, head)}


def test_plan_unstamped_shop():
    got = outcomes(build(load([f"{EX}/shop"])), build(load([f"{EX}/shop-after"])))
    assert got == {
        ("ConfigMap/shop-config", "Deployment/api"): "STALE",
        ("ConfigMap/shop-config", "Deployment/edge"): "STALE",
        ("ConfigMap/shop-config", "CronJob/nightly-report"): "next-run",
        ("Secret/payments-api-key", "Deployment/api"): "STALE",
        ("Secret/payments-api-key", "Deployment/payments"): "rolls-unsafe",
    }


def _stamped(path):
    docs, _ = stamp(build(load([path])))
    return build(parse_text(yaml.safe_dump_all(docs), path))


def test_stamp_makes_every_deployment_roll():
    got = outcomes(_stamped(f"{EX}/shop"), _stamped(f"{EX}/shop-after"))
    assert got[("ConfigMap/shop-config", "Deployment/api")] == "rolls"
    assert got[("ConfigMap/shop-config", "Deployment/edge")] == "rolls-unsafe"  # no readinessProbe
    assert "STALE" not in got.values()


def test_stamp_is_idempotent_and_skips_jobs_and_unrelated_docs():
    g = build(load([f"{EX}/shop"]))
    docs, changes = stamp(g)
    assert {c[0] for c in changes} == {"Deployment/api", "Deployment/edge", "Deployment/web", "Deployment/payments"}
    cron = [d for d in docs if d["kind"] == "CronJob"][0]
    assert "annotations" not in (cron["spec"]["jobTemplate"]["spec"]["template"].get("metadata") or {})
    g2 = build(parse_text(yaml.safe_dump_all(docs), "s"))
    _, changes2 = stamp(g2)
    assert changes2 == []
    assert [f.rule for f in scan(g2) if f.rule in ("CR001", "CR005")] == []
    # original objects untouched
    assert all(HASH_ANNOTATION not in str(d.obj) for d in g.docs)


def test_restart_only_skips_volume_only_workloads():
    _, changes = stamp(build(load([f"{EX}/shop"])), restart_only=True)
    assert "Deployment/web" not in {c[0] for c in changes}


def test_stale_hash_detected():
    docs, _ = stamp(build(load([f"{EX}/shop"])))
    after = parse_text(open(f"{EX}/shop-after/config.yaml").read(), "c")
    workloads = [d for d in parse_text(yaml.safe_dump_all(docs), "w") if d.kind not in ("ConfigMap", "Secret")]
    fs = [f for f in scan(build(after + workloads)) if f.rule == "CR005"]
    assert {f.workload for f in fs} == {"Deployment/api", "Deployment/edge", "Deployment/payments"}


def test_deleted_config_breaks_and_unchanged_config_ignored():
    base = build(load([f"{EX}/shop"]))
    head_docs = [d for d in load([f"{EX}/shop"]) if d.name != "payments-api-key"]
    got = outcomes(base, build(head_docs))
    assert set(got.values()) == {"BREAKS"}
    assert outcomes(base, build(load([f"{EX}/shop"]))) == {}


def test_ondelete_is_held():
    sts = """
kind: ConfigMap
metadata: {name: cfg}
data: {A: "%s"}
---
kind: StatefulSet
metadata: {name: db}
spec:
  updateStrategy: {type: OnDelete}
  template:
    metadata: {annotations: {checksum/config: "%s"}}
    spec: {containers: [{name: db, envFrom: [{configMapRef: {name: cfg}}]}]}
"""
    got = outcomes(build(parse_text(sts % ("1", "a"), "b")), build(parse_text(sts % ("2", "b"), "h")))
    assert got == {("ConfigMap/cfg", "StatefulSet/db"): "held"}


def test_load_git(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "k8s").mkdir()
    (tmp_path / "k8s/cm.yaml").write_text("kind: ConfigMap\nmetadata: {name: c}\ndata: {A: '1'}\n")
    (tmp_path / "k8s/d.yaml").write_text(
        "kind: Deployment\nmetadata: {name: d}\nspec: {template: {spec: {containers: "
        "[{name: x, envFrom: [{configMapRef: {name: c}}]}]}}}\n")
    git("add", ".")
    git("commit", "-qm", "init")
    (tmp_path / "k8s/cm.yaml").write_text("kind: ConfigMap\nmetadata: {name: c}\ndata: {A: '2'}\n")
    base = build(load_git("HEAD", ["k8s"], cwd=str(tmp_path)))
    head = build(load([str(tmp_path / "k8s")]))
    assert outcomes(base, head) == {("ConfigMap/c", "Deployment/d"): "STALE"}
