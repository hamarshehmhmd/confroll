import json
import os
import subprocess
import sys

from confroll.cli import main

from conftest import EX, ROOT


def run(*args, stdin=None):
    p = subprocess.run([sys.executable, "-m", "confroll", *args], capture_output=True, text=True, input=stdin,
                       cwd=ROOT)
    return p.returncode, p.stdout, p.stderr


def test_version():
    rc, out, _ = run("--version")
    assert rc == 0 and out.strip() == "confroll 0.1.0"


def test_scan_text_exit_codes():
    rc, out, _ = run("scan", "examples/shop")
    assert rc == 1 and "CR001" in out and "3 error(s)" in out
    assert run("scan", "examples/shop", "--fail-on", "none")[0] == 0
    assert run("scan", "examples/shop", "--min-severity", "warning", "--fail-on", "warning")[0] == 1


def test_scan_json_sarif_github():
    rc, out, _ = run("scan", "examples/shop", "--format", "json")
    data = json.loads(out)
    assert data["workloads"] == 5 and data["configs"] == 3 and len(data["findings"]) == 6
    s = json.loads(run("scan", "examples/shop", "--format", "sarif")[1])
    assert s["version"] == "2.1.0" and s["runs"][0]["results"][0]["ruleId"] == "CR001"
    gh = run("scan", "examples/shop", "--format", "github")[1]
    assert gh.startswith("::error file=examples/shop/workloads.yaml,title=confroll CR001::")


def test_scan_stdin_and_missing_path():
    rc, out, _ = run("scan", "-", stdin=open(os.path.join(EX, "shop", "workloads.yaml")).read())
    assert rc == 1 and "CR006" in out and "CR001" in out  # configs live elsewhere: still stale
    rc, _, err = run("scan", "nope/")
    assert rc != 0 and "no such file" in err


def test_plan_cli_formats():
    rc, out, _ = run("plan", "--base", "examples/shop", "--head", "examples/shop-after")
    assert rc == 1 and "STALE" in out and "3 workload(s) will NOT get the change" in out
    rc, out, _ = run("plan", "--base", "examples/shop", "--head", "examples/shop-after", "--format", "markdown",
                     "--fail-on", "none")
    assert rc == 0 and "| `ConfigMap/shop-config` | FEATURE_NEW_CHECKOUT, nginx.conf | `Deployment/api` | **STALE**" in out
    items = json.loads(run("plan", "--base", "examples/shop", "--head", "examples/shop-after", "--format", "json")[1])
    assert len(items["items"]) == 5
    assert "sk_test" not in out  # never print secret values


def test_plan_requires_inputs():
    rc, _, err = run("plan")
    assert rc != 0 and "--base and --head" in err


def test_stamp_roundtrip_through_pipe(tmp_path):
    rc, before, err = run("stamp", "examples/shop")
    assert rc == 0 and "Deployment/api: config-hash (none) ->" in err
    after = run("stamp", "examples/shop-after")[1]
    (tmp_path / "b.yaml").write_text(before)
    (tmp_path / "a.yaml").write_text(after)
    rc, out, _ = run("plan", "--base", str(tmp_path / "b.yaml"), "--head", str(tmp_path / "a.yaml"))
    assert rc == 0 and "0 workload(s) will NOT get the change" in out
    # stamping already-stamped output from stdin changes nothing
    rc, again, err = run("stamp", "-", stdin=before)
    assert rc == 0 and again == before and err == ""


def test_stamp_output_file(tmp_path):
    out = tmp_path / "o.yaml"
    assert main(["stamp", os.path.join(EX, "shop"), "-o", str(out)]) == 0
    assert "confroll.dev/config-hash" in out.read_text()


def test_stale_cli():
    rc, out, _ = run("-n", "shop", "stale", "examples/cluster-dump.yaml")
    assert rc == 1 and "STALE Deployment/api -n shop" in out
    assert "kubectl rollout restart deployment/api -n shop" in out and "11h 29m" in out
    data = json.loads(run("stale", "examples/cluster-dump.yaml", "--format", "json", "--fail-on", "none")[1])
    assert len(data["stale"]) == 2


def test_color_always():
    out = run("--color", "always", "scan", "examples/shop")[1]
    assert "\x1b[31m" in out
