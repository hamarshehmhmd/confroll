"""Render the confroll narrated demo: real command output + edge-tts narration.

Usage: python demo/render.py   (needs ffmpeg, edge-tts, Pillow, network)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
OUT = os.path.join(ROOT, "demo.mp4")
SDKBIN = "/tmp/crvenv/bin"
SANDBOX = "/tmp/crdemo"
W, H, FPS = 1280, 720, 24
VOICE = "en-US-AndrewNeural"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BG, FG = (24, 26, 33), (220, 223, 228)
ANSI = {"31": (255, 95, 95), "32": (95, 215, 120), "33": (240, 200, 80),
        "2": (130, 135, 145), "36": (90, 200, 230), "35": (200, 140, 230), "1": FG}
PROMPT = "dev@laptop ~/shop $ "

SCENES = [
    {
        "kind": "title",
        "title": "confroll",
        "sub": "your ConfigMap changed.\nyour pods didn't.",
        "say": "This is conf roll, a small open source command line tool for a Kubernetes problem that bites almost "
               "every team: you edit a config map or a secret, and the running pods never see the change.",
    },
    {
        "kind": "bullets",
        "title": "The problem",
        "bullets": [
            "env / envFrom values are frozen when the container starts",
            "subPath mounts never refresh, even for live-reload apps",
            "Nothing restarts the pods; kubernetes#22368, open since 2016",
            "Half-fixes: checksum annotations, Reloader, kustomize hashes",
            "   ...until one workload is missed and keeps the old value",
        ],
        "say": "Values read through env or env from are frozen when the container starts, and files mounted with "
               "sub path never refresh at all. Kubernetes does not restart anything. The upstream issue has been open "
               "since twenty sixteen. Teams patch it with checksum annotations, Reloader or kustomize hashes, and then "
               "one workload gets missed, and keeps the old feature flag or the revoked key.",
    },
    {
        "kind": "term",
        "cmds": ["confroll --color always scan examples/shop"],
        "show": ["confroll scan examples/shop"],
        "say": "conf roll scan reads plain manifests, or rendered helm and kustomize output. In this example shop, "
               "the A P I reads its config through env from and a secret through env, and nothing triggers a rollout. "
               "The nginx edge mounts its config with sub path. Those are errors. The web app only mounts a volume, "
               "which refreshes. And the payments service is handled by a Reloader annotation, but it uses the "
               "recreate strategy with no readiness probe, so one bad config takes it fully down.",
    },
    {
        "kind": "term",
        "cmds": ["confroll --color always plan --base examples/shop --head examples/shop-after"],
        "show": ["confroll plan --base examples/shop --head examples/shop-after"],
        "say": "conf roll plan compares two versions, or your branch against origin main with git base. This pull "
               "request flips a feature flag, changes nginx, and rotates the Stripe key. The plan shows the A P I and "
               "the edge proxy stay stale, the cron job gets it on its next run, and payments rolls, but unsafely. "
               "It shows changed key names, never secret values.",
    },
    {
        "kind": "term",
        "cmds": ["confroll stamp examples/shop > before.yaml 2>/dev/null; confroll stamp examples/shop-after > after.yaml",
                 "confroll --color always plan --base before.yaml --head after.yaml | grep -E '^\\S|rolls|STALE|next-run|NOT'"],
        "show": ["confroll stamp examples/shop-after > after.yaml",
                 "confroll plan --base before.yaml --head after.yaml"],
        "say": "The fix is conf roll stamp. It adds a hash of the config each pod template reads, so a config edit "
               "changes the template and Kubernetes does a normal rolling update with your readiness probes. Pipe "
               "kustomize or helm template output through it before kubectl apply. Now the same change rolls "
               "everything that needs it.",
    },
    {
        "kind": "term",
        "cmds": ["confroll --color always -n shop stale examples/cluster-dump.yaml"],
        "show": ["kubectl get pods,cm,secrets -n shop -o yaml --show-managed-fields | confroll -n shop stale -"],
        "say": "And for a live cluster, conf roll stale reads kubectl output with managed fields, and lists the running "
               "pods that started before the config they read was last written, with the rollout restart command to fix "
               "them. Label only edits are ignored. In C I, this is tested end to end on a real kind cluster.",
    },
    {
        "kind": "bullets",
        "title": "What you get, and limits",
        "bullets": [
            "Knows Reloader, Wave, checksum/*, kustomize hashes, immutable",
            "text / JSON / SARIF / GitHub annotations / markdown plans",
            "Static: config created at runtime shows as 'not in manifests'",
            "Trusts checksum annotations; only verifies its own hash",
            "stale compares timestamps; it doesn't look inside containers",
        ],
        "say": "It recognizes Reloader, Wave, checksum annotations, kustomize hashes and immutable config, and "
               "outputs text, JSON, SARIF, GitHub annotations or a markdown plan for pull requests. The limits: it is "
               "static, so config created at runtime is only reported as missing, it trusts other checksum annotations, "
               "and stale compares timestamps rather than looking inside containers.",
    },
    {
        "kind": "bullets",
        "title": "Try it",
        "bullets": [
            "curl -fsSL https://raw.githubusercontent.com/",
            "   hamarshehmhmd/confroll/main/install.sh | sh",
            "confroll scan k8s/   ·   confroll plan --git-base origin/main k8s/",
            "github.com/hamarshehmhmd/confroll",
            "GNU AGPL-3.0-or-later (commercial licenses available)",
        ],
        "say": "Install it with the one line installer and run conf roll scan on your manifests. It is licensed under "
               "the GNU Affero General Public License version 3 or later, with commercial licenses available from the "
               "author. It's on GitHub at hamarsheh m h m d slash conf roll.",
    },
]


async def tts(text, path):
    import edge_tts
    for attempt in range(8):
        try:
            await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)
            return
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            if attempt == 7:
                raise
            await asyncio.sleep(5)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        text=True,
    )
    return float(out.strip())


EXPORTS = {}


def run_cmd(cmd, cwd):
    if cmd.startswith("export ") and "=" in cmd:
        k, v = cmd[7:].split("=", 1)
        EXPORTS[k] = v
        return ""
    env = dict(os.environ, FORCE_COLOR="1", **EXPORTS)
    env.pop("NO_COLOR", None)
    env["PATH"] = SDKBIN + os.pathsep + env["PATH"]
    env["COLUMNS"] = "120"
    p = subprocess.run(
        ["bash", "-c", "shopt -s nullglob; " + cmd],
        cwd=cwd, capture_output=True, text=True, env=env,
    )
    return (p.stdout + p.stderr).rstrip("\n")


def parse_ansi(line):
    spans, color, bold = [], FG, False
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = re.match(r"\x1b\[([0-9;]*)m", part)
        if m:
            for code in (m.group(1) or "0").split(";"):
                if code == "0":
                    color, bold = FG, False
                elif code == "1":
                    bold = True
                elif code in ANSI:
                    color = ANSI[code]
        elif part:
            spans.append((part, color, bold))
    return spans


class Term:
    def __init__(self):
        self.font = ImageFont.truetype(MONO, 14)
        self.bold = ImageFont.truetype(MONO_B, 14)
        self.cw = self.font.getlength("M")
        self.lh = 20
        self.cols = int((W - 80) / self.cw)

    def wrap(self, spans):
        lines, cur, n = [], [], 0
        for text, c, b in spans:
            while text:
                room = self.cols - n
                if len(text) <= room:
                    cur.append((text, c, b)); n += len(text); text = ""
                else:
                    cut = text.rfind(" ", 0, room + 1)
                    cut = cut if cut > 0 else room
                    cur.append((text[:cut], c, b)); lines.append(cur); cur, n = [], 0
                    text = "    " + text[cut:].lstrip(" ")
        lines.append(cur)
        return lines

    def frame(self, lines, cursor=False):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([20, 14, W - 20, H - 14], 12, fill=(30, 33, 41), outline=(60, 64, 76))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([40 + i * 24, 30, 54 + i * 24, 44], fill=c)
        d.text((W // 2, 37), "~ — bash", font=ImageFont.truetype(SANS, 15), fill=(150, 155, 165), anchor="mm")
        visible = lines[-30:]
        y = 64
        for ln in visible:
            x = 40
            for text, c, b in ln:
                d.text((x, y), text, font=self.bold if b else self.font, fill=c)
                x += self.cw * len(text)
            y += self.lh
        if cursor:
            last = visible[-1] if visible else []
            x = 40 + self.cw * sum(len(t) for t, _, _ in last)
            d.rectangle([x, y - self.lh + 3, x + self.cw - 2, y - 4], fill=FG)
        return img


def card(scene):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if scene["kind"] == "title":
        d.text((W // 2, 280), scene["title"], font=ImageFont.truetype(MONO_B, 78), fill=(95, 215, 120), anchor="mm")
        d.multiline_text((W // 2, 420), scene["sub"], font=ImageFont.truetype(SANS, 32), fill=FG, anchor="mm",
                         align="center", spacing=12)
        d.text((W // 2, 620), "github.com/hamarshehmhmd/confroll  ·  scan · plan · stamp · stale  ·  GNU AGPL-3.0",
               font=ImageFont.truetype(SANS, 20), fill=(150, 155, 165), anchor="mm")
    else:
        d.text((90, 90), scene["title"], font=ImageFont.truetype(SANS_B, 52), fill=(95, 215, 120))
        f = ImageFont.truetype(SANS, 29)
        y = 200
        for b in scene["bullets"]:
            if b and not b.startswith("   "):
                d.text((90, y), "•", font=f, fill=(240, 200, 80))
            d.text((130, y), b.strip(), font=f, fill=FG)
            y += 58 if b.startswith("   ") or not b else 62
    return img


class Writer:
    def __init__(self, path, audio):
        self.p = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", audio,
             "-af", "apad", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-shortest", path],
            stdin=subprocess.PIPE,
        )
        self.n = 0

    def put(self, img, count=1):
        data = img.tobytes()
        for _ in range(count):
            self.p.stdin.write(data)
        self.n += count

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            sys.exit("ffmpeg failed")


def _cap_output(out):
    lines = out.splitlines()
    if len(lines) > 26:
        lines = lines[:24] + ["…", lines[-1]]
    return "\n".join(lines)


def render_scene(i, scene, workdir):
    workdir = scene.get("cwd", SANDBOX).replace("EX", os.path.join(SANDBOX, "examples"))
    # leaky-client lives in examples/, but we also want repo-style paths.
    # Use SANDBOX as cwd when the command references examples/...
    if scene.get("cwd") == "EX":
        workdir = SANDBOX
    audio = os.path.join(BUILD, f"s{i}.mp3")
    if not os.path.exists(audio):
        asyncio.run(tts(scene["say"], audio))
    total = int((duration(audio) + 0.7) * FPS)
    path = os.path.join(BUILD, f"s{i}.mp4")
    w = Writer(path, audio)
    if scene["kind"] != "term":
        w.put(card(scene), total)
        w.close()
        return path
    t = Term()
    shown = scene.get("show", scene["cmds"])
    outputs = [_cap_output(run_cmd(c, workdir)) for c in scene["cmds"]]
    # If show has more lines than cmds (a command displayed as several visual lines
    # that map to one executed command), join show into one typed command per output.
    if len(shown) != len(outputs):
        if len(outputs) == 1:
            shown = [" ".join(s.rstrip("\\").strip() for s in shown)]
        elif len(shown) > len(outputs):
            # first N-1 outputs map 1:1; remaining show lines are the last command
            shown = list(shown[: len(outputs) - 1]) + [
                " ".join(s.rstrip("\\").strip() for s in shown[len(outputs) - 1 :])
            ]
    steps = list(zip(shown, outputs))
    typing = sum(len(s) for s, _ in steps) / 32 * FPS
    pause = max(6, int((total - typing) / max(1, len(steps))) - 4)
    lines = []
    for cmd, out in steps:
        prompt = [(PROMPT, (95, 215, 120), True)]
        for k in range(0, len(cmd) + 1, 2):
            frame = t.frame(lines + t.wrap(prompt + [(cmd[:k], FG, False)]), cursor=True)
            w.put(frame)
        lines += t.wrap(prompt + [(cmd, FG, False)])
        for ol in out.splitlines():
            lines += t.wrap(parse_ansi(ol))
        lines.append([])
        hold = min(pause, max(0, total - w.n))
        w.put(t.frame(lines), hold)
    final = t.frame(lines + t.wrap([(PROMPT, (95, 215, 120), True)]), cursor=True)
    if w.n < total:
        w.put(final, total - w.n)
    w.close()
    return path


def main():
    os.makedirs(BUILD, exist_ok=True)
    if os.path.exists(SANDBOX):
        shutil.rmtree(SANDBOX)
    os.makedirs(SANDBOX)
    shutil.copytree(os.path.join(ROOT, "examples"), os.path.join(SANDBOX, "examples"))
    parts = [render_scene(i, s, SANDBOX) for i, s in enumerate(SCENES)]
    with open(os.path.join(BUILD, "list.txt"), "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
         os.path.join(BUILD, "list.txt"), "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", OUT],
        check=True,
    )
    with open(os.path.join(BUILD, "narration.txt"), "w") as fh:
        fh.write("\n\n".join(s["say"] for s in SCENES) + "\n")
    print(OUT, round(duration(OUT), 1), "s")


if __name__ == "__main__":
    main()
