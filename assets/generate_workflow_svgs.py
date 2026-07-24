#!/usr/bin/env python3
"""Generate light/dark workflow + architecture SVGs for the root README."""
from pathlib import Path

OUT = Path(__file__).resolve().parent
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

LIGHT = dict(
    node_fill="#ffffff", node_stroke="#d0d7de", title="#1f2328", sub="#59636e",
    purple_fill="#f1eafe", purple_stroke="#c9b3f5", purple_title="#5b3fbc", purple_sub="#8a76c9",
    green_fill="#e9f7ef", green_stroke="#8fd6ac", green_title="#1a7f4b", green_sub="#549f77",
    entry_fill="#f6f8fa", entry_stroke="#d0d7de", entry_title="#59636e", entry_sub="#818b95",
    arrow="#6e7781", ret="#8b949e", label="#59636e", legend="#59636e",
)
DARK = dict(
    node_fill="#161b22", node_stroke="#3d444d", title="#e6edf3", sub="#9198a1",
    purple_fill="#221a38", purple_stroke="#6e40c9", purple_title="#c3aaf9", purple_sub="#9d89d8",
    green_fill="#122b1d", green_stroke="#2ea043", green_title="#72dd9d", green_sub="#5aa878",
    entry_fill="#0d1117", entry_stroke="#3d444d", entry_title="#9198a1", entry_sub="#6e7781",
    arrow="#8b949e", ret="#8b949e", label="#9198a1", legend="#9198a1",
)

# System-architecture palettes: solid fills, (fill, stroke, title, sub) per box.
# Brand orange #FF6B35 (from the intro deck) marks the Brain; everything else
# stays in the deck's warm-neutral family (#555555, #F6F6F4, #B8BCC4).
SYS_LIGHT = dict(
    zone1=("#f6f6f4", "#d5d3cd", "#555555"),
    zone2=("#fdeee6", "#f2c3aa", "#c24d1e"),
    plain=("#ffffff", "#b8bcc4", "#1f2328", "#555555"),
    hub=("#3f4348", "#26282c", "#ffffff", "#cfd2d6"),
    brain=("#ff6b35", "#e0521d", "#ffffff", "#ffe1d4"),
    frontend=("#ffffff", "#f2c3aa", "#c24d1e", "#bd7a58"),
    infra=("#767b83", "#5a5f66", "#ffffff", "#e3e5e8"),
    arrow="#5b5b5b", ret="#5b5b5b", label="#555555", legend="#555555",
)
SYS_DARK = dict(
    zone1=("#16181c", "#3a3d42", "#9aa0a6"),
    zone2=("#2a180e", "#7a4426", "#ff8f63"),
    plain=("#1e2126", "#4a4e55", "#e6e8eb", "#9aa0a6"),
    hub=("#495059", "#8a929c", "#ffffff", "#c9ced4"),
    brain=("#e85d2c", "#ff8a5e", "#ffffff", "#ffd9c7"),
    frontend=("#1d1712", "#7a4426", "#ff9d73", "#c98d6e"),
    infra=("#4d5157", "#75797f", "#e8eaec", "#b4b8bd"),
    arrow="#8b949e", ret="#8b949e", label="#9aa0a6", legend="#9aa0a6",
)

W = 992
NODE_W, NODE_H = 160, 64
XS = [24, 220, 416, 612, 808]          # left edges; centers +80

LEGEND = "solid arrow = forward · dashed = review sends work back · purple = adversarial review"

PLAIN_KEYS = {"fill": "node_fill", "stroke": "node_stroke", "title": "title", "sub": "sub"}


def box(p, x, y, w, h, title, sub, kind="plain"):
    key = lambda part: p[PLAIN_KEYS[part] if kind == "plain" else f"{kind}_{part}"]
    dash = ' stroke-dasharray="5 4"' if kind == "entry" else ""
    cx = x + w // 2
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="{key("fill")}" stroke="{key("stroke")}" stroke-width="1.5"{dash}/>'
        f'<text x="{cx}" y="{y + 28}" text-anchor="middle" font-size="14" '
        f'font-weight="600" fill="{key("title")}">{title}</text>'
        f'<text x="{cx}" y="{y + 47}" text-anchor="middle" font-size="11" '
        f'fill="{key("sub")}">{sub}</text>'
    )


def sbox(colors, x, y, w, h, title, sub):
    fill, stroke, tcol, scol = colors
    cx = x + w // 2
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
        f'<text x="{cx}" y="{y + 28}" text-anchor="middle" font-size="14" '
        f'font-weight="600" fill="{tcol}">{title}</text>'
        f'<text x="{cx}" y="{y + 47}" text-anchor="middle" font-size="11" '
        f'fill="{scol}">{sub}</text>'
    )


def szone(colors, x, y, w, h, label, note=""):
    fill, stroke, lcol = colors
    extra = (f'<text x="{x + w - 24}" y="{y + 27}" text-anchor="end" '
             f'font-size="11" fill="{lcol}">{note}</text>') if note else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
        f'<text x="{x + 20}" y="{y + 27}" font-size="11" font-weight="600" '
        f'letter-spacing="1.5" fill="{lcol}">{label}</text>{extra}'
    )


def node(p, x, title, sub, kind="plain", y=64):
    return box(p, x, y, NODE_W, NODE_H, title, sub, kind)


def fwd_arrow(p, x1, x2, y):
    return (f'<line x1="{x1 + 2}" y1="{y}" x2="{x2 - 3}" y2="{y}" '
            f'stroke="{p["arrow"]}" stroke-width="1.5" marker-end="url(#fwd)"/>')


def ret_arc(p, x_from, x_to, y_from, depth, label, label_x, label_y):
    return (
        f'<path d="M {x_from} {y_from} C {x_from} {depth}, {x_to} {depth}, {x_to} {y_from + 8}" '
        f'fill="none" stroke="{p["ret"]}" stroke-width="1.5" stroke-dasharray="5 4" '
        f'marker-end="url(#ret)"/>'
        f'<text x="{label_x}" y="{label_y}" text-anchor="middle" font-size="11" '
        f'fill="{p["label"]}">{label}</text>'
    )


def link(p, path, label, label_x, label_y, anchor="middle"):
    """Non-workflow connector: a thin line with a small label."""
    return (
        f'<path d="{path}" fill="none" stroke="{p["arrow"]}" stroke-width="1.5" '
        f'marker-end="url(#fwd)"/>'
        f'<text x="{label_x}" y="{label_y}" text-anchor="{anchor}" font-size="11" '
        f'fill="{p["label"]}">{label}</text>'
    )


def svg(p, body, aria, h, legend=True):
    head = f'<text x="24" y="32" font-size="12" fill="{p["legend"]}">{LEGEND}</text>' if legend else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" '
        f'font-family="{FONT}" role="img" aria-label="{aria}">'
        f'<defs>'
        f'<marker id="fwd" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{p["arrow"]}"/></marker>'
        f'<marker id="ret" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{p["ret"]}"/></marker>'
        f'</defs>{head}{body}</svg>\n'
    )


def experiment(p):
    parts = [
        node(p, XS[0], "Plan", "write the experiment plan"),
        node(p, XS[1], "Design review", "the plan must pass", "purple"),
        node(p, XS[2], "Execute", "run locally or in a sandbox"),
        node(p, XS[3], "Results review", "the report must pass", "purple"),
        node(p, XS[4], "Complete", "findings recorded", "green"),
    ]
    for i in range(4):
        parts.append(fwd_arrow(p, XS[i] + NODE_W, XS[i + 1], 96))
    parts.append(ret_arc(p, 300, 104, 128, 190, "revise the plan", 202, 194))
    parts.append(ret_arc(p, 680, 496, 128, 190, "fix run or report", 588, 194))
    parts.append(ret_arc(p, 704, 80, 128, 250, "experiment proved faulty", 392, 236))
    return svg(p, "".join(parts),
               "Experiment workflow: plan, design review, execute, results review, "
               "complete; rejected reviews send work back to execution or planning", 256)


LENSES = [
    ("Amplify · what worked", True),
    ("Avoid · what failed", True),
    ("Entropy · weird bets", True),
    ("Agent-chosen lens", False),
    ("Agent-chosen lens", False),
]


def project(p):
    row_y, mid = 108, 140
    parts = [
        node(p, XS[0], "Completed work", "a wave of experiments", "entry", row_y),
        node(p, XS[2], "Synthesis", "report · graph · spec", y=row_y),
        node(p, XS[3], "Reflection review", "adversarial check", "purple", row_y),
        node(p, XS[4], "Publish", "sets up the next wave", "green", row_y),
    ]
    # Lens cluster in column 2: five pills, three core + two designed per project.
    parts.append(f'<text x="300" y="52" text-anchor="middle" font-size="11" '
                 f'fill="{p["label"]}">3 core lenses + 2 designed for this project</text>')
    pill_ys = [59, 93, 127, 161, 195]
    fan_in_ys = [124, 132, 140, 148, 156]     # spread over Synthesis's left edge
    for (text, core), y, fy in zip(LENSES, pill_ys, fan_in_ys):
        dash = "" if core else ' stroke-dasharray="5 4"'
        tcol, weight = (p["title"], 600) if core else (p["sub"], 400)
        parts.append(
            f'<rect x="220" y="{y}" width="160" height="26" rx="13" '
            f'fill="{p["node_fill"]}" stroke="{p["node_stroke"]}" stroke-width="1.5"{dash}/>'
            f'<text x="300" y="{y + 17}" text-anchor="middle" font-size="11" '
            f'font-weight="{weight}" fill="{tcol}">{text}</text>')
        cy = y + 13
        parts.append(f'<line x1="186" y1="{mid}" x2="214" y2="{cy}" '
                     f'stroke="{p["arrow"]}" stroke-width="1.2" marker-end="url(#fwd)"/>')
        parts.append(f'<line x1="382" y1="{cy}" x2="412" y2="{fy}" '
                     f'stroke="{p["arrow"]}" stroke-width="1.2" marker-end="url(#fwd)"/>')
    for i in (2, 3):
        parts.append(fwd_arrow(p, XS[i] + NODE_W, XS[i + 1], mid))
    parts.append(ret_arc(p, 680, 496, 172, 226, "revise synthesis", 588, 236))
    # Deep return to the lens cluster: ends under the bottom pill, not the node row.
    parts.append(
        f'<path d="M 704 172 C 704 288, 300 288, 300 229" fill="none" '
        f'stroke="{p["ret"]}" stroke-width="1.5" stroke-dasharray="5 4" '
        f'marker-end="url(#ret)"/>'
        f'<text x="502" y="284" text-anchor="middle" font-size="11" '
        f'fill="{p["label"]}">lenses fall short</text>')
    return svg(p, "".join(parts),
               "Project workflow: a completed wave of experiments fans out to five "
               "reflection lenses (Amplify what works, Avoid what failed, Entropy and "
               "weird bets, plus two agent-chosen), then synthesis, adversarial review, "
               "and publish; rejected reviews send work back to synthesis or the lenses", 296)


def system(s):
    parts = [
        szone(s["zone1"], 24, 40, 520, 260, "YOUR MACHINE"),
        szone(s["zone2"], 584, 40, 384, 260, "SERVICE SIDE",
              note="brain can run locally"),
        sbox(s["plain"], 48, 84, 472, 56, "Agent platform",
             "Claude Code · Codex · Cursor · Gemini CLI · OpenCode"),
        sbox(s["plain"], 48, 196, 160, 64, "Research repo", "source · retained evidence"),
        sbox(s["frontend"], 608, 84, 336, 56, "Frontend UI", "supervision · lifecycle controls"),
        sbox(s["brain"], 608, 196, 336, 64, "Brain", "records · gates · providers"),
        sbox(s["infra"], 608, 340, 200, 56, "Cloud sandboxes", "Lambda · Thunder · Modal"),
        sbox(s["infra"], 828, 340, 140, 56, "Data services", "DB · blobs · MLflow"),
        link(s, "M 128 140 L 128 192", "", 0, 0),
        link(s, "M 524 112 C 572 112, 572 228, 602 228", "HTTP MCP · project key", 566, 170, "start"),
        link(s, "M 776 140 L 776 192", "", 0, 0),
        link(s, "M 284 140 C 284 328, 430 350, 600 350", "SSH commands", 348, 317, "start"),
        link(s, "M 400 140 C 400 382, 500 382, 600 382", "rsync pulls", 428, 348, "end"),
        link(s, "M 484 140 C 484 428, 660 428, 822 388", "presigned uploads", 660, 442, "middle"),
        link(s, "M 700 260 L 700 334", "provisions", 710, 320, "start"),
        link(s, "M 898 260 L 898 334", "", 0, 0),
    ]
    return svg(s, "".join(parts),
               "System architecture: agent platforms on your machine connect directly to "
               "the brain over HTTP MCP with a project-scoped key; the brain owns "
               "research records, workflow gates, data stores, MLflow, and sandbox "
               "providers. The UI supervises the brain. The agent runs SSH commands on "
               "cloud sandboxes, pulls retained outputs itself with rsync, and moves "
               "heavy bytes over presigned URLs", 460,
               legend=False)


for name, palette, sys_palette in (("light", LIGHT, SYS_LIGHT), ("dark", DARK, SYS_DARK)):
    (OUT / f"experiment-workflow-{name}.svg").write_text(experiment(palette))
    (OUT / f"project-workflow-{name}.svg").write_text(project(palette))
    (OUT / f"system-architecture-{name}.svg").write_text(system(sys_palette))
print("wrote", *sorted(f.name for f in OUT.glob("*.svg")))
