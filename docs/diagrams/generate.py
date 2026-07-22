"""Generate README SVG diagrams. Run: python docs/diagrams/generate.py"""

from __future__ import annotations

import os


def _save(name: str, svg: str) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"wrote {path}")


def cache_impact_svg() -> str:
    """Before/after bar chart for repeated read_file calls."""
    width = 720
    height = 360
    bar_w = 64
    gap = 80
    margin_left = 100
    margin_top = 60
    max_h = 220
    y_base = margin_top + max_h

    # Before: 5 upstream calls; After: 1 upstream + 4 cached
    before_h = max_h
    after_upstream_h = max_h * 0.2
    after_cached_h = max_h * 0.8

    bars = [
        ("Without cache", "#ef4444", before_h, "5 upstream calls"),
        ("With cache (upstream)", "#f59e0b", after_upstream_h, "1 upstream call"),
        ("With cache (hits)", "#10b981", after_cached_h, "4 cache hits"),
    ]

    rects = []
    labels = []
    x = margin_left
    for label, color, h, desc in bars:
        y = y_base - h
        rects.append(f'<rect x="{x}" y="{y}" width="{bar_w}" height="{h}" rx="6" fill="{color}"/>')
        labels.append(f'<text x="{x + bar_w/2}" y="{y_base + 20}" text-anchor="middle" font-size="13" fill="#334155">{label}</text>')
        labels.append(f'<text x="{x + bar_w/2}" y="{y - 10}" text-anchor="middle" font-size="13" font-weight="600" fill="#334155">{desc}</text>')
        x += bar_w + gap

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <rect width="100%" height="100%" fill="#f8fafc"/>
  <text x="{width/2}" y="36" text-anchor="middle" font-size="18" font-weight="700" fill="#0f172a">Repeated read_file(path) calls in a long session</text>
  <line x1="{margin_left}" y1="{y_base}" x2="{margin_left + (bar_w + gap) * len(bars) - gap}" y2="{y_base}" stroke="#94a3b8" stroke-width="2"/>
  {''.join(rects)}
  {''.join(labels)}
</svg>'''
    return svg.strip()


def latency_svg() -> str:
    """Latency comparison for cached vs upstream."""
    width = 720
    height = 300
    margin_top = 60
    y_base = 240

    # Scenario: 10 repeated calls, upstream ~200ms, cache ~1ms
    upstream_total = 2000
    cache_total = 200 + 9 * 1
    scale = 180 / max(upstream_total, cache_total)

    bars = [
        ("Upstream every time", "#ef4444", upstream_total, f"~{upstream_total} ms"),
        ("toolcall-cache", "#10b981", cache_total, f"~{cache_total} ms"),
    ]

    bar_h = 48
    gap = 40
    x = 120
    rects = []
    labels = []
    for label, color, value, desc in bars:
        w = value * scale
        rects.append(f'<rect x="{x}" y="{margin_top}" width="{w}" height="{bar_h}" rx="6" fill="{color}"/>')
        labels.append(f'<text x="{x}" y="{margin_top + bar_h + 22}" font-size="14" font-weight="600" fill="#334155">{label}</text>')
        labels.append(f'<text x="{x + w/2}" y="{margin_top + bar_h/2 + 5}" text-anchor="middle" font-size="14" font-weight="600" fill="white">{desc}</text>')
        x += max(w, 160) + gap

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <rect width="100%" height="100%" fill="#f8fafc"/>
  <text x="{width/2}" y="36" text-anchor="middle" font-size="18" font-weight="700" fill="#0f172a">Latency: 10 repeated deterministic tool calls</text>
  {''.join(rects)}
  {''.join(labels)}
</svg>'''
    return svg.strip()


def architecture_svg() -> str:
    """Box-and-arrow architecture diagram."""
    width = 720
    height = 340
    boxes = [
        ("Agent / MCP client", 40, 130, 140, 80, "#e2e8f0", "#475569"),
        ("toolcall-cache\nproxy", 290, 110, 140, 120, "#bfdbfe", "#1d4ed8"),
        ("Upstream\nMCP server", 540, 130, 140, 80, "#e2e8f0", "#475569"),
        ("SQLite cache", 290, 260, 140, 50, "#dcfce7", "#15803d"),
    ]

    rect_tags = []
    text_tags = []
    for label, x, y, w, h, fill, stroke in boxes:
        rect_tags.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        lines = label.split("\n")
        if len(lines) == 1:
            text_tags.append(f'<text x="{x + w/2}" y="{y + h/2 + 5}" text-anchor="middle" font-size="14" font-weight="600" fill="#0f172a">{label}</text>')
        else:
            text_tags.append(f'<text x="{x + w/2}" y="{y + h/2 - 6}" text-anchor="middle" font-size="14" font-weight="600" fill="#0f172a">{lines[0]}</text>')
            text_tags.append(f'<text x="{x + w/2}" y="{y + h/2 + 16}" text-anchor="middle" font-size="14" font-weight="600" fill="#0f172a">{lines[1]}</text>')

    arrows = [
        (180, 170, 290, 170, "request"),
        (430, 170, 540, 170, "forward (on miss)"),
        (360, 230, 360, 260, "store / lookup"),
        (610, 170, 430, 170, "response"),
    ]
    arrow_tags = []
    for x1, y1, x2, y2, label in arrows:
        arrow_tags.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"/>')
        mx = (x1 + x2) / 2
        my = (y1 + y2) / 2
        arrow_tags.append(f'<text x="{mx}" y="{my - 8}" text-anchor="middle" font-size="12" fill="#64748b">{label}</text>')

    marker = '''<defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L9,3 z" fill="#64748b" />
    </marker>
  </defs>'''

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <rect width="100%" height="100%" fill="#f8fafc"/>
  <text x="{width/2}" y="36" text-anchor="middle" font-size="18" font-weight="700" fill="#0f172a">Where toolcall-cache sits in your MCP stack</text>
  {marker}
  {''.join(rect_tags)}
  {''.join(text_tags)}
  {''.join(arrow_tags)}
</svg>'''
    return svg.strip()


def banner_svg() -> str:
    """Hero banner for the README."""
    width = 800
    height = 220
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#1e40af"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="url(#bg)" rx="12"/>
  <text x="{width/2}" y="95" text-anchor="middle" font-size="42" font-weight="800" fill="#ffffff">toolcall-cache</text>
  <text x="{width/2}" y="140" text-anchor="middle" font-size="18" fill="#bfdbfe">Stop paying for the same MCP tool call twice</text>
  <text x="{width/2}" y="175" text-anchor="middle" font-size="14" fill="#94a3b8">Local · SQLite · stdio + HTTP · pipx installable</text>
</svg>'''
    return svg.strip()


def main() -> None:
    _save("banner.svg", banner_svg())
    _save("cache-impact.svg", cache_impact_svg())
    _save("latency.svg", latency_svg())
    _save("architecture.svg", architecture_svg())


if __name__ == "__main__":
    main()
