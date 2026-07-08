#!/usr/bin/env python3
"""Generate light/dark workflow SVGs for the root README."""
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

W, H = 992, 256
NODE_W, NODE_H, NODE_Y = 160, 64, 64
XS = [24, 220, 416, 612, 808]          # left edges; centers +80
MID_Y = NODE_Y + NODE_H // 2           # 96
BOT_Y = NODE_Y + NODE_H                # 128

LEGEND = "solid arrow = forward · dashed = review sends work back · purple = adversarial review"


def node(p, x, title, sub, kind="plain"):
    fill, stroke = p[f"{kind}_fill" if kind != "plain" else "node_fill"], p[f"{kind}_stroke" if kind != "plain" else "node_stroke"]
    tcol = p[f"{kind}_title" if kind != "plain" else "title"]
    scol = p[f"{kind}_sub" if kind != "plain" else "sub"]
    dash = ' stroke-dasharray="5 4"' if kind == "entry" else ""
    cx = x + NODE_W // 2
    return (
        f'<rect x="{x}" y="{NODE_Y}" width="{NODE_W}" height="{NODE_H}" rx="10" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>'
        f'<text x="{cx}" y="{NODE_Y + 28}" text-anchor="middle" font-size="14" '
        f'font-weight="600" fill="{tcol}">{title}</text>'
        f'<text x="{cx}" y="{NODE_Y + 47}" text-anchor="middle" font-size="11" '
        f'fill="{scol}">{sub}</text>'
    )


def fwd_arrow(p, x1, x2):
    return (f'<line x1="{x1 + 2}" y1="{MID_Y}" x2="{x2 - 3}" y2="{MID_Y}" '
            f'stroke="{p["arrow"]}" stroke-width="1.5" marker-end="url(#fwd)"/>')


def ret_arc(p, x_from, x_to, depth, label, label_x, label_y):
    return (
        f'<path d="M {x_from} {BOT_Y} C {x_from} {depth}, {x_to} {depth}, {x_to} {BOT_Y + 8}" '
        f'fill="none" stroke="{p["ret"]}" stroke-width="1.5" stroke-dasharray="5 4" '
        f'marker-end="url(#ret)"/>'
        f'<text x="{label_x}" y="{label_y}" text-anchor="middle" font-size="11" '
        f'fill="{p["label"]}">{label}</text>'
    )


def svg(p, body, aria):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="{FONT}" role="img" aria-label="{aria}">'
        f'<defs>'
        f'<marker id="fwd" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{p["arrow"]}"/></marker>'
        f'<marker id="ret" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{p["ret"]}"/></marker>'
        f'</defs>'
        f'<text x="24" y="32" font-size="12" fill="{p["legend"]}">{LEGEND}</text>'
        f"{body}</svg>\n"
    )


def experiment(p):
    parts = [
        node(p, XS[0], "Plan", "write the experiment plan"),
        node(p, XS[1], "Design review", "the plan must pass", "purple"),
        node(p, XS[2], "Execute", "run in a sandbox"),
        node(p, XS[3], "Results review", "the report must pass", "purple"),
        node(p, XS[4], "Complete", "findings recorded", "green"),
    ]
    for i in range(4):
        parts.append(fwd_arrow(p, XS[i] + NODE_W, XS[i + 1]))
    parts.append(ret_arc(p, 300, 104, 190, "revise the plan", 202, 194))
    parts.append(ret_arc(p, 680, 496, 190, "fix run or report", 588, 194))
    parts.append(ret_arc(p, 704, 80, 250, "experiment proved faulty", 392, 236))
    return svg(p, "".join(parts),
               "Experiment workflow: plan, design review, execute, results review, "
               "complete; rejected reviews send work back to execution or planning")


def project(p):
    parts = [
        node(p, XS[0], "Completed work", "a wave of experiments", "entry"),
        node(p, XS[1], "Reflection fan-out", "5 lenses in parallel"),
        node(p, XS[2], "Synthesis", "report · graph · spec"),
        node(p, XS[3], "Reflection review", "adversarial check", "purple"),
        node(p, XS[4], "Publish", "sets up the next wave", "green"),
    ]
    for i in range(4):
        parts.append(fwd_arrow(p, XS[i] + NODE_W, XS[i + 1]))
    parts.append(ret_arc(p, 680, 496, 185, "revise synthesis", 588, 190))
    parts.append(ret_arc(p, 704, 300, 235, "lenses fall short", 502, 224))
    return svg(p, "".join(parts),
               "Project workflow: completed experiments fan out to five reflection "
               "lenses, then synthesis, adversarial review, and publish; rejected "
               "reviews send work back to synthesis or the lenses")


OUT.mkdir(exist_ok=True)
for name, palette in (("light", LIGHT), ("dark", DARK)):
    (OUT / f"experiment-workflow-{name}.svg").write_text(experiment(palette))
    (OUT / f"project-workflow-{name}.svg").write_text(project(palette))
print("wrote", *sorted(f.name for f in OUT.glob("*.svg")))
