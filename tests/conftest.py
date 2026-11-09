import os
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")


@pytest.fixture
def write(tmp_path):
    def _w(name, text):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
        return str(p)
    return _w


def deployment(name="app", annotations="", template_annotations="", container_extra="", spec_extra="",
               volumes="", kind="Deployment"):
    return f"""
apiVersion: apps/v1
kind: {kind}
metadata:
  name: {name}
{('  annotations:' + chr(10) + annotations) if annotations else ''}
spec:
  replicas: 3
{spec_extra}
  selector: {{matchLabels: {{app: {name}}}}}
  template:
    metadata:
      labels: {{app: {name}}}
{('      annotations:' + chr(10) + template_annotations) if template_annotations else ''}
    spec:
      containers:
        - name: main
          image: busybox
          readinessProbe: {{exec: {{command: [true]}}}}
{container_extra}
{volumes}
"""


CM = """
apiVersion: v1
kind: ConfigMap
metadata: {name: cfg}
data: {A: "1"}
"""
