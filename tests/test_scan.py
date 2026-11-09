from confroll.analyze import scan
from confroll.model import build, load, parse_text

from conftest import CM, EX, deployment

ENVFROM = "          envFrom: [{configMapRef: {name: cfg}}]"


def rules(text, ns="default"):
    return [(f.rule, f.severity, f.workload) for f in scan(build(parse_text(text, "t.yaml"), ns))]


def test_envfrom_without_trigger_is_error():
    assert rules(CM + "---" + deployment(container_extra=ENVFROM)) == [("CR001", "error", "Deployment/app")]


def test_subpath_is_error_and_mentions_never_refreshed():
    text = CM + "---" + deployment(
        container_extra="          volumeMounts: [{name: v, mountPath: /etc/x, subPath: A}]",
        volumes="      volumes: [{name: v, configMap: {name: cfg}}]")
    fs = scan(build(parse_text(text, "t")))
    assert [f.rule for f in fs] == ["CR001"] and "never refreshed" in fs[0].message


def test_plain_volume_is_info():
    text = CM + "---" + deployment(container_extra="          volumeMounts: [{name: v, mountPath: /etc/x}]",
                                   volumes="      volumes: [{name: v, configMap: {name: cfg}}]")
    assert rules(text) == [("CR002", "info", "Deployment/app")]


def test_triggers_silence_cr001():
    cases = [
        dict(annotations="    reloader.stakater.com/auto: \"true\""),
        dict(annotations="    configmap.reloader.stakater.com/reload: \"other, cfg\""),
        dict(annotations="    configmap.reloader.stakater.com/auto: \"true\""),
        dict(annotations="    wave.pusher.com/update-on-config-change: \"true\""),
        dict(template_annotations="        checksum/config: abc123"),
    ]
    for kw in cases:
        assert rules(CM + "---" + deployment(container_extra=ENVFROM, **kw)) == [], kw


def test_reloader_list_for_other_name_does_not_count():
    text = CM + "---" + deployment(container_extra=ENVFROM,
                                   annotations="    configmap.reloader.stakater.com/reload: \"something-else\"")
    assert [r[0] for r in rules(text)] == ["CR001"]


def test_reloader_search_needs_match_on_config():
    dep = deployment(container_extra=ENVFROM, annotations="    reloader.stakater.com/search: \"true\"")
    assert [r[0] for r in rules(CM + "---" + dep)] == ["CR001"]
    matched = CM.replace("metadata: {name: cfg}", "metadata: {name: cfg, annotations: {reloader.stakater.com/match: 'true'}}")
    assert rules(matched + "---" + dep) == []


def test_kustomize_hash_and_immutable():
    k = CM.replace("name: cfg", "name: cfg-5c8bkd7mft") + "---" + deployment(
        container_extra="          envFrom: [{configMapRef: {name: cfg-5c8bkd7mft}}]")
    assert rules(k) == []
    imm = CM + "immutable: true\n---" + deployment(container_extra=ENVFROM)
    assert rules(imm) == []


def test_missing_ref_is_info_unless_optional():
    assert rules(deployment(container_extra=ENVFROM))[0][:2] == ("CR006", "info")
    opt = "          envFrom: [{configMapRef: {name: cfg, optional: true}}]"
    assert "CR006" not in [r[0] for r in rules(deployment(container_extra=opt))]


def test_ondelete_and_unsafe_rollouts():
    sts = deployment(kind="StatefulSet", container_extra=ENVFROM, spec_extra="  updateStrategy: {type: OnDelete}",
                     template_annotations="        checksum/config: x")
    assert ("CR003", "warning", "StatefulSet/app") in rules(CM + "---" + sts)
    rec = deployment(container_extra=ENVFROM, spec_extra="  strategy: {type: Recreate}",
                     template_annotations="        checksum/config: x")
    assert [r[0] for r in rules(CM + "---" + rec)] == ["CR004"]
    mu = deployment(container_extra=ENVFROM, spec_extra="  strategy: {rollingUpdate: {maxUnavailable: 100%}}",
                    template_annotations="        checksum/config: x")
    assert [r[0] for r in rules(CM + "---" + mu)] == ["CR004"]
    noprobe = deployment(container_extra=ENVFROM, template_annotations="        checksum/config: x").replace(
        "          readinessProbe: {exec: {command: [true]}}\n", "")
    assert [r[0] for r in rules(CM + "---" + noprobe)] == ["CR004"]


def test_jobs_skip_and_bare_pod_warns():
    cron = """
kind: CronJob
metadata: {name: c}
spec: {schedule: "* * * * *", jobTemplate: {spec: {template: {spec: {containers: [{name: x, envFrom: [{configMapRef: {name: cfg}}]}]}}}}}
"""
    assert rules(CM + "---" + cron) == []
    pod = "kind: Pod\nmetadata: {name: p}\nspec: {containers: [{name: x, envFrom: [{configMapRef: {name: cfg}}]}]}\n"
    assert rules(CM + "---\n" + pod) == [("CR007", "warning", "Pod/p")]


def test_namespaces_are_separate():
    other = CM.replace("{name: cfg}", "{name: cfg, namespace: other}")
    found = rules(other + "---" + deployment(container_extra=ENVFROM))
    assert ("CR006", "info", "Deployment/app") in found


def test_example_shop():
    fs = scan(build(load([f"{EX}/shop"])))
    assert sorted({(f.rule, f.workload) for f in fs}) == [
        ("CR001", "Deployment/api"), ("CR001", "Deployment/edge"), ("CR002", "Deployment/web"),
        ("CR004", "Deployment/payments")]
