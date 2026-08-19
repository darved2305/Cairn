#!/usr/bin/env python3
"""Render a ``.excalidraw`` scene to a self-contained SVG.

Every diagram in `docs/assets/diagrams/` is committed twice: the
``.excalidraw`` file is the editable source, and the ``.svg`` next to it is
what `README.md` embeds. This script is the link between them, so a diagram
can be opened in excalidraw.com, edited, saved back over the source, and
re-exported without hand-editing any SVG.

    uv run python scripts/excalidraw_to_svg.py docs/assets/diagrams/*.excalidraw

Scope, stated so nobody expects more: this renders the element subset those
diagrams actually use — rectangle, ellipse, diamond, line, arrow, and text —
in Excalidraw's "architect" style (``roughness: 0``). It is not a general
Excalidraw renderer and deliberately does not attempt freedraw, images,
bindings, or the hand-drawn stroke generator.

Two conventions this repo's diagrams rely on, both stored in Excalidraw's own
``customData`` field so the source file stays valid for the web editor:

    customData.weight  -> font-weight for a text element (default 400)
    customData.mono    -> render the text element in the monospace stack
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

SANS = (
    "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "Inter, Helvetica, Arial, sans-serif"
)
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', 'Cascadia Code', Consolas, monospace"

PADDING = 28
LINE_HEIGHT = 1.25

DASH = {"dashed": "9 7", "dotted": "1.5 5"}


def _bounds(elements: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for el in elements:
        x, y = float(el["x"]), float(el["y"])
        if el["type"] in ("line", "arrow"):
            for px, py in el.get("points", [[0, 0]]):
                xs.append(x + px)
                ys.append(y + py)
        else:
            xs.append(x)
            ys.append(y)
            xs.append(x + float(el.get("width", 0)))
            ys.append(y + float(el.get("height", 0)))
    return min(xs), min(ys), max(xs), max(ys)


def _stroke_attrs(el: dict[str, Any]) -> str:
    parts = [
        f'stroke="{el.get("strokeColor", "#334155")}"',
        f'stroke-width="{el.get("strokeWidth", 2)}"',
        'stroke-linecap="round"',
        'stroke-linejoin="round"',
    ]
    dash = DASH.get(el.get("strokeStyle", "solid"))
    if dash:
        parts.append(f'stroke-dasharray="{dash}"')
    opacity = float(el.get("opacity", 100)) / 100.0
    if opacity < 1:
        parts.append(f'opacity="{opacity:g}"')
    return " ".join(parts)


def _fill(el: dict[str, Any]) -> str:
    background = el.get("backgroundColor", "transparent")
    if background in ("transparent", "", None):
        return "none"
    return str(background)


def _radius(el: dict[str, Any]) -> float:
    if not el.get("roundness"):
        return 0.0
    w, h = abs(float(el.get("width", 0))), abs(float(el.get("height", 0)))
    return min(16.0, w / 4.0, h / 4.0)


def _render_shape(el: dict[str, Any]) -> str:
    x, y = float(el["x"]), float(el["y"])
    w, h = float(el.get("width", 0)), float(el.get("height", 0))
    common = f'fill="{_fill(el)}" {_stroke_attrs(el)}'
    if el["type"] == "rectangle":
        r = _radius(el)
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r:.1f}" {common}/>'
    if el["type"] == "ellipse":
        return (
            f'<ellipse cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" '
            f'rx="{w / 2:.1f}" ry="{h / 2:.1f}" {common}/>'
        )
    if el["type"] == "diamond":
        pts = [
            (x + w / 2, y),
            (x + w, y + h / 2),
            (x + w / 2, y + h),
            (x, y + h / 2),
        ]
        joined = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        return f'<polygon points="{joined}" {common}/>'
    raise ValueError(f"unsupported shape {el['type']!r}")


def _arrowhead(ax: float, ay: float, bx: float, by: float, color: str, size: float = 11.0) -> str:
    """Filled triangular head on the segment a -> b, pointing at b."""
    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    # 22-degree half-angle reads as a clean architect arrow at this stroke weight.
    spread = 0.404
    back_x, back_y = bx - ux * size, by - uy * size
    nx, ny = -uy * size * spread, ux * size * spread
    pts = (
        f"{bx:.1f},{by:.1f} {back_x + nx:.1f},{back_y + ny:.1f} {back_x - nx:.1f},{back_y - ny:.1f}"
    )
    return f'<polygon points="{pts}" fill="{color}" stroke="none"/>'


def _render_linear(el: dict[str, Any]) -> str:
    x, y = float(el["x"]), float(el["y"])
    pts = [(x + float(px), y + float(py)) for px, py in el.get("points", [])]
    if len(pts) < 2:
        return ""
    color = el.get("strokeColor", "#334155")
    body = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    out = [f'<polyline points="{body}" fill="none" {_stroke_attrs(el)}/>']
    if el.get("endArrowhead") == "arrow":
        out.append(_arrowhead(*pts[-2], *pts[-1], color))
    if el.get("startArrowhead") == "arrow":
        out.append(_arrowhead(*pts[1], *pts[0], color))
    return "".join(out)


def _render_text(el: dict[str, Any]) -> str:
    size = float(el.get("fontSize", 16))
    custom = el.get("customData") or {}
    family = MONO if custom.get("mono") else SANS
    weight = custom.get("weight", 400)
    align = el.get("textAlign", "left")
    anchor = {"left": "start", "center": "middle", "right": "end"}[align]
    x = float(el["x"])
    width = float(el.get("width", 0))
    if align == "center":
        x += width / 2
    elif align == "right":
        x += width
    lines = str(el.get("text", "")).split("\n")
    step = size * LINE_HEIGHT
    # Excalidraw's y is the top of the text block; shift to the first baseline.
    top = float(el["y"]) + size * 0.92
    opacity = float(el.get("opacity", 100)) / 100.0
    out = [
        f'<text x="{x:.1f}" y="{top:.1f}" font-family="{family}" font-size="{size:g}" '
        f'font-weight="{weight}" fill="{el.get("strokeColor", "#111827")}" '
        f'text-anchor="{anchor}" xml:space="preserve"'
        + (f' opacity="{opacity:g}"' if opacity < 1 else "")
        + ">"
    ]
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else step
        out.append(f'<tspan x="{x:.1f}" dy="{dy:.1f}">{escape(line)}</tspan>')
    out.append("</text>")
    return "".join(out)


def render(scene: dict[str, Any]) -> str:
    elements = [el for el in scene.get("elements", []) if not el.get("isDeleted")]
    if not elements:
        raise ValueError("scene has no elements")
    min_x, min_y, max_x, max_y = _bounds(elements)
    vx, vy = min_x - PADDING, min_y - PADDING
    vw = (max_x - min_x) + PADDING * 2
    vh = (max_y - min_y) + PADDING * 2

    body: list[str] = []
    for el in elements:
        kind = el["type"]
        if kind in ("rectangle", "ellipse", "diamond"):
            body.append(_render_shape(el))
        elif kind in ("line", "arrow"):
            body.append(_render_linear(el))
        elif kind == "text":
            body.append(_render_text(el))
        else:
            raise ValueError(f"unsupported element type {kind!r}")

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw:.0f}" height="{vh:.0f}" '
        f'viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}" role="img">'
        f'<rect x="{vx:.1f}" y="{vy:.1f}" width="{vw:.1f}" height="{vh:.1f}" fill="#FFFFFF"/>'
        + "".join(body)
        + "</svg>\n"
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for raw in argv:
        source = Path(raw)
        scene = json.loads(source.read_text(encoding="utf-8"))
        target = source.with_suffix(".svg")
        target.write_text(render(scene), encoding="utf-8", newline="\n")
        print(f"{source} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
