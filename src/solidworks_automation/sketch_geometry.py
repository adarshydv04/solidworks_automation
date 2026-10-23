"""Pure 2D sketch geometry: turns rich sketch entities into SolidWorks primitives.

Slots, rectangles and polygons become lines and arcs; per-entity patterns and
mirrors are expanded into copies. Nothing here touches SolidWorks, so it is
unit-tested directly. All values are millimetres in sketch space.

Primitive kinds produced: line, centerline, polyline, circle, arc.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

Point = tuple[float, float]
Mapper = Callable[[list[float]], Point]

POINT_FIELDS = ("start", "end", "center", "corner1", "corner2", "center1", "center2")


def _identity(p: list[float]) -> Point:
    if len(p) != 2:
        raise ValueError(f"Expected a 2D sketch point [x, y], got {p}. Set model_coordinates=true for [x, y, z].")
    return float(p[0]), float(p[1])


def expand_entities(entities: list[dict], to_sketch: Mapper | None = None) -> list[dict]:
    """Map points into sketch space, then expand shapes, patterns and mirrors into primitives."""
    to_sketch = to_sketch or _identity
    primitives: list[dict] = []
    for entity in entities:
        mapped = _map_entity(entity, to_sketch)
        shapes = _to_primitives(mapped)
        copies = list(shapes)
        pattern = mapped.get("pattern")
        if pattern:
            copies = [_transform(s, t, flips) for t, flips in _pattern_transforms(pattern) for s in shapes]
        mirror = mapped.get("mirror_about")
        if mirror:
            reflect = _reflection(*mirror)
            copies += [_transform(s, reflect, True) for s in copies]
        primitives += copies
    return primitives


# --- point mapping ------------------------------------------------------------------


def _map_entity(entity: dict, to_sketch: Mapper) -> dict:
    out = dict(entity)
    for field in POINT_FIELDS:
        if out.get(field) is not None:
            out[field] = to_sketch(out[field])
    if out.get("points") is not None:
        out["points"] = [to_sketch(p) for p in out["points"]]
    if out.get("mirror_about") is not None:
        out["mirror_about"] = [to_sketch(p) for p in out["mirror_about"]]
    pattern = out.get("pattern")
    if pattern:
        pattern = dict(pattern)
        if pattern.get("center") is not None:
            pattern["center"] = to_sketch(pattern["center"])
        for key in ("spacing", "spacing2"):
            if pattern.get(key) is not None:
                pattern[key] = _map_vector(pattern[key], to_sketch)
        out["pattern"] = pattern
    return out


def _map_vector(v: list[float], to_sketch: Mapper) -> Point:
    origin = to_sketch([0.0] * len(v))
    tip = to_sketch(list(v))
    return tip[0] - origin[0], tip[1] - origin[1]


# --- shapes -----------------------------------------------------------------------


def _to_primitives(e: dict) -> list[dict]:
    kind = e.get("type")
    if kind in ("line", "centerline"):
        return [{"type": kind, "start": e["start"], "end": e["end"]}]
    if kind == "polyline":
        return [{"type": "polyline", "points": list(e["points"]), "closed": e.get("closed", True)}]
    if kind == "circle":
        return [{"type": "circle", "center": e["center"], "radius": float(e["radius"])}]
    if kind == "arc":
        return [{"type": "arc", "center": e["center"], "start": e["start"], "end": e["end"],
                 "clockwise": bool(e.get("clockwise"))}]
    if kind == "rectangle":
        (x1, y1), (x2, y2) = e["corner1"], e["corner2"]
        return [{"type": "polyline", "points": [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], "closed": True}]
    if kind == "center_rectangle":
        (cx, cy), hw, hh = e["center"], e["width"] / 2, e["height"] / 2
        corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
        angle = math.radians(e.get("angle", 0.0))
        points = [(cx + x * math.cos(angle) - y * math.sin(angle), cy + x * math.sin(angle) + y * math.cos(angle))
                  for x, y in corners]
        return [{"type": "polyline", "points": points, "closed": True}]
    if kind == "polygon":
        (cx, cy), sides = e["center"], int(e["sides"])
        r = e["radius"] if e.get("inscribed", True) else e["radius"] / math.cos(math.pi / sides)
        start = math.radians(e.get("angle", 0.0))
        points = [(cx + r * math.cos(start + 2 * math.pi * i / sides), cy + r * math.sin(start + 2 * math.pi * i / sides))
                  for i in range(sides)]
        return [{"type": "polyline", "points": points, "closed": True}]
    if kind == "slot":
        return _slot(e["center1"], e["center2"], e["width"] / 2)
    raise ValueError(f"Unknown sketch entity type '{kind}'.")


def _slot(c1: Point, c2: Point, r: float) -> list[dict]:
    """Straight slot: two lines and two semicircular end arcs."""
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return [{"type": "circle", "center": c1, "radius": r}]
    nx, ny = -dy / length * r, dx / length * r
    a1, b1 = (c1[0] + nx, c1[1] + ny), (c1[0] - nx, c1[1] - ny)
    a2, b2 = (c2[0] + nx, c2[1] + ny), (c2[0] - nx, c2[1] - ny)
    return [
        {"type": "line", "start": a1, "end": a2},
        {"type": "arc", "center": c2, "start": a2, "end": b2, "clockwise": True},
        {"type": "line", "start": b2, "end": b1},
        {"type": "arc", "center": c1, "start": b1, "end": a1, "clockwise": True},
    ]


# --- patterns & mirrors ----------------------------------------------------------------

Transform = Callable[[Point], Point]


def _pattern_transforms(pattern: dict) -> list[tuple[Transform, bool]]:
    kind = pattern.get("type")
    if kind == "linear":
        (sx, sy) = pattern["spacing"]
        count2 = int(pattern.get("count2") or 1)
        (tx, ty) = pattern.get("spacing2") or (0.0, 0.0)
        return [
            (_translation(i * sx + j * tx, i * sy + j * ty), False)
            for j in range(count2)
            for i in range(int(pattern["count"]))
        ]
    if kind == "circular":
        count = int(pattern["count"])
        total = float(pattern.get("total_angle", 360.0))
        step = total / count if math.isclose(total % 360.0, 0.0, abs_tol=1e-9) else total / (count - 1)
        return [(_rotation(pattern["center"], math.radians(step * i)), False) for i in range(count)]
    raise ValueError(f"Unknown pattern type '{kind}'.")


def _translation(dx: float, dy: float) -> Transform:
    return lambda p: (p[0] + dx, p[1] + dy)


def _rotation(center: Point, angle: float) -> Transform:
    cos, sin = math.cos(angle), math.sin(angle)
    cx, cy = center

    def rotate(p: Point) -> Point:
        x, y = p[0] - cx, p[1] - cy
        return cx + x * cos - y * sin, cy + x * sin + y * cos

    return rotate


def _reflection(a: Point, b: Point) -> Transform:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-18:
        raise ValueError("mirror_about needs two different points.")

    def reflect(p: Point) -> Point:
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length_sq
        fx, fy = a[0] + t * dx, a[1] + t * dy
        return 2 * fx - p[0], 2 * fy - p[1]

    return reflect


def _transform(prim: dict, f: Transform, flips: bool) -> dict:
    out: dict[str, Any] = dict(prim)
    for field in ("start", "end", "center"):
        if field in out:
            out[field] = f(out[field])
    if "points" in out:
        out["points"] = [f(p) for p in out["points"]]
    if flips and out["type"] == "arc":
        out["clockwise"] = not out["clockwise"]
    return out
