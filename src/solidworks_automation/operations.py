"""SolidWorks operations: JSON-friendly arguments in, JSON-friendly dicts out.

This is the only module that talks to the SolidWorks API. It runs wherever
SolidWorks runs (the local Windows machine, or the bridge inside a Windows VM),
always on the COM thread. Lengths are millimetres and angles are degrees at
this boundary; SolidWorks itself works in metres and radians.
"""

from __future__ import annotations

import base64
import math
import os
import tempfile
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import constants as c
from .com import (
    SolidWorksError,
    as_list,
    call,
    double_array,
    empty,
    find_running_app,
    get,
    is_installed,
    launch_app,
    nothing,
    out_bool,
    out_long,
    out_str,
    put,
)
from .safety import output_dir, resolve_write_path
from .sketch_geometry import expand_entities

HIDDEN_FEATURE_TYPES = {
    "OriginProfileFeature",
    "MaterialFolder",
    "EqnFolder",
    "DetailCabinet",
    "MateGroup",
    "Attribute",
}


def _m(mm: float) -> float:
    return float(mm) * c.MM


def _mm(m: float) -> float:
    return round(float(m) / c.MM, 6)


def _point3(p: list[float]) -> tuple[float, float, float]:
    if len(p) != 3:
        raise SolidWorksError(f"Expected a 3D point [x, y, z] in mm, got {p}.")
    return _m(p[0]), _m(p[1]), _m(p[2])


def _point2(p: list[float]) -> tuple[float, float]:
    if len(p) != 2:
        raise SolidWorksError(f"Expected a 2D sketch point [x, y] in mm, got {p}.")
    return _m(p[0]), _m(p[1])


def find_template_file(suffix: str, year: int | None, install_dir: str | None) -> Path | None:
    """Look for a template in the standard SolidWorks folders.

    Searches this release's ProgramData templates first, then any release, then the
    install folder (which ships tutorial templates). Prefers 'Part'/'Assembly' named files.
    """
    solidworks_data = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SOLIDWORKS"
    roots = []
    if year:
        roots.append(solidworks_data / f"SOLIDWORKS {year}" / "templates")
    roots.append(solidworks_data)
    if install_dir:
        roots.append(Path(install_dir) / "lang")
    preferred = {".prtdot": "part", ".asmdot": "assembly"}.get(suffix)

    for root in roots:
        if not root.is_dir():
            continue
        # Newest release folder first ("SOLIDWORKS 2026" sorts after "2025"), then preferred names first.
        matches = sorted(root.rglob(f"*{suffix}"), key=str, reverse=True)
        matches.sort(key=lambda p: p.stem.lower() != preferred)
        if matches:
            return matches[0]
    return None


class SolidWorksOps:
    """Stateful session against one SolidWorks instance."""

    def __init__(self) -> None:
        self._app: Any = None
        # Features this server created, keyed by document title. Only these can be deleted.
        self._created: dict[str, set[str]] = {}

    # --- connection ---------------------------------------------------------

    def _alive(self) -> bool:
        if self._app is None:
            return False
        try:
            get(self._app, "RevisionNumber")
            return True
        except SolidWorksError:
            self._app = None
            return False

    def _app_or_raise(self) -> Any:
        if not self._alive():
            self._app = find_running_app()
        if self._app is None:
            raise SolidWorksError(
                "SolidWorks is not running. Call sw_connect so the user can approve starting it."
            )
        return self._app

    def status(self) -> dict:
        if not self._alive():
            self._app = find_running_app()
        if self._app is None:
            return {"installed": is_installed(), "running": False, "documents": []}
        app = self._app
        revision = str(call(app, "RevisionNumber"))
        major = int(revision.split(".")[0]) if revision.split(".")[0].isdigit() else None
        active = get(app, "ActiveDoc")
        return {
            "installed": True,
            "running": True,
            "revision": revision,
            "release": f"SOLIDWORKS {major + 1992}" if major else None,
            "active_document": call(active, "GetTitle") if active else None,
            "documents": self._documents(),
            "output_dir": str(output_dir()),
        }

    def connect(self, launch: bool) -> dict:
        if not self._alive():
            self._app = find_running_app()
        launched = False
        if self._app is None:
            if not launch:
                return {"connected": False, "launched": False, "running": False, "installed": is_installed()}
            self._app = launch_app()
            launched = True
        return {"connected": True, "launched": launched, **self.status()}

    def _documents(self) -> list[dict]:
        docs = []
        model = call(self._app, "GetFirstDocument")
        while model is not None:
            if get(model, "Visible"):
                docs.append(self._describe_doc(model))
            model = call(model, "GetNext")
        return docs

    def _describe_doc(self, model: Any) -> dict:
        doc_type = call(model, "GetType")
        return {
            "title": call(model, "GetTitle"),
            "path": call(model, "GetPathName") or None,
            "type": c.DOC_TYPES.get(doc_type, str(doc_type)),
            "unsaved_changes": bool(call(model, "GetSaveFlag")),
        }

    def _doc(self, doc: str | None) -> Any:
        app = self._app_or_raise()
        if not doc:
            model = get(app, "ActiveDoc")
            if model is None:
                raise SolidWorksError("No document is open. Use sw_new_document or sw_open first.")
            return model
        model = call(app, "GetOpenDocumentByName", doc)
        if model is None:
            open_titles = [d["title"] for d in self._documents()]
            raise SolidWorksError(f"No open document named '{doc}'. Open documents: {open_titles}.")
        call(app, "ActivateDoc3", call(model, "GetTitle"), False, 0, out_long())
        return model

    def _part(self, doc: str | None) -> Any:
        model = self._doc(doc)
        if call(model, "GetType") != c.DOC_PART:
            raise SolidWorksError("This operation needs a part document.")
        return model

    def _assembly(self, doc: str | None) -> Any:
        model = self._doc(doc)
        if call(model, "GetType") != c.DOC_ASSEMBLY:
            raise SolidWorksError("This operation needs an assembly document.")
        return model

    # --- documents ------------------------------------------------------------

    def _release_year(self) -> int | None:
        major = str(call(self._app, "RevisionNumber")).split(".")[0]
        return int(major) + 1992 if major.isdigit() else None

    def _template(self, kind: str) -> str:
        """Find a document template: env override, SolidWorks settings, then the usual folders on disk."""
        app = self._app
        doc_type = c.DOC_PART if kind == "part" else c.DOC_ASSEMBLY
        pref = c.PREF_DEFAULT_TEMPLATE_PART if kind == "part" else c.PREF_DEFAULT_TEMPLATE_ASSEMBLY
        candidates = [os.environ.get(f"SW_MCP_{kind.upper()}_TEMPLATE"), call(app, "GetUserPreferenceStringValue", pref)]
        try:
            candidates.append(call(app, "GetDocumentTemplate", doc_type, "", 0, 0.0, 0.0))
        except SolidWorksError:
            pass  # not available on every release
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate

        found = find_template_file(c.TEMPLATE_SUFFIXES[kind], self._release_year(), call(app, "GetExecutablePath"))
        if found is None:
            raise SolidWorksError(
                f"No {kind} template found. In SolidWorks set Tools > Options > System Options > Default Templates, "
                f"or set SW_MCP_{kind.upper()}_TEMPLATE to the full path of a {c.TEMPLATE_SUFFIXES[kind]} file."
            )
        return str(found)

    def new_document(self, kind: str) -> dict:
        app = self._app_or_raise()
        if kind not in c.TEMPLATE_SUFFIXES:
            raise SolidWorksError("kind must be 'part' or 'assembly'.")
        template = self._template(kind)
        model = call(app, "NewDocument", template, 0, 0.0, 0.0)
        if model is None:
            raise SolidWorksError(f"SolidWorks could not create a new {kind} from '{template}'.")
        return self._describe_doc(model)

    def open_document(self, path: str, read_only: bool) -> dict:
        app = self._app_or_raise()
        file = Path(path).expanduser()
        doc_type = c.DOC_EXTENSIONS.get(file.suffix.lower())
        if doc_type is None:
            raise SolidWorksError("Only .sldprt, .sldasm and .slddrw files can be opened.")
        if not file.is_file():
            raise SolidWorksError(f"File not found: {file}")
        options = c.OPEN_SILENT | (c.OPEN_READ_ONLY if read_only else 0)
        errors, warnings = out_long(), out_long()
        model = call(app, "OpenDoc6", str(file.resolve()), doc_type, options, "", errors, warnings)
        if model is None:
            raise SolidWorksError(f"SolidWorks could not open the file (error code {errors.value}).")
        return self._describe_doc(model)

    def close_document(self, doc: str | None, discard_changes: bool) -> dict:
        model = self._doc(doc)
        title = call(model, "GetTitle")
        if call(model, "GetSaveFlag") and not discard_changes:
            raise SolidWorksError(
                f"'{title}' has unsaved changes. Save it first, or pass discard_changes=true after the user agrees."
            )
        call(self._app, "CloseDoc", title)
        self._created.pop(title, None)
        return {"closed": title}

    # --- selection helpers ------------------------------------------------------

    def _features(self, model: Any) -> list[Any]:
        feats = []
        feat = call(model, "FirstFeature")
        while feat is not None:
            feats.append(feat)
            feat = call(feat, "GetNextFeature")
        return feats

    def _feature_by_name(self, model: Any, name: str) -> Any:
        for feat in self._features(model):
            if get(feat, "Name") == name:
                return feat
        raise SolidWorksError(f"No feature named '{name}'.")

    def _standard_plane(self, model: Any, plane: str) -> Any:
        planes = [f for f in self._features(model) if call(f, "GetTypeName2") == "RefPlane"]
        return planes[c.STANDARD_PLANES.index(plane)]

    def _select_plane(self, model: Any, plane: str, append: bool = False, mark: int = 0) -> None:
        key = plane.strip().lower()
        if key in c.STANDARD_PLANES:
            feat = self._standard_plane(model, key)
        else:
            feat = self._feature_by_name(model, plane)
        if not call(feat, "Select2", append, mark):
            raise SolidWorksError(f"Could not select plane '{plane}'.")

    def _select_entity(self, model: Any, spec: dict, append: bool, mark: int) -> None:
        """Select a face/edge/vertex at a model point, or a plane/feature by name."""
        ext = get(model, "Extension")
        entity_type = spec.get("type", "face").upper()
        if spec.get("name"):
            ok = call(ext, "SelectByID2", spec["name"], entity_type, 0.0, 0.0, 0.0, append, mark, nothing(), 0)
            where = f"named '{spec['name']}'"
        elif spec.get("point") is not None:
            x, y, z = _point3(spec["point"])
            ok = call(ext, "SelectByID2", "", entity_type, x, y, z, append, mark, nothing(), 0)
            where = f"at {spec['point']} mm"
        else:
            raise SolidWorksError("An entity needs either 'point' or 'name'.")
        if not ok:
            raise SolidWorksError(f"No {entity_type.lower()} found {where}.")

    def _select_edges(self, model: Any, edges: str | list[list[float]]) -> int:
        call(model, "ClearSelection2", True)
        if edges == "all":
            for body in as_list(call(model, "GetBodies2", 0, True)):
                for edge in as_list(call(body, "GetEdges")):
                    call(edge, "Select4", True, nothing())
        else:
            for i, point in enumerate(edges):
                self._select_entity(model, {"type": "edge", "point": point}, append=i > 0, mark=0)
        count = call(get(model, "SelectionManager"), "GetSelectedObjectCount2", -1)
        if not count:
            raise SolidWorksError("No edges were selected.")
        return count

    def _record(self, model: Any, feature: Any, name: str | None) -> str:
        if feature is None:
            feature = call(model, "FeatureByPositionReverse", 0)
        if name:
            put(feature, "Name", name)
        feature_name = get(feature, "Name")
        self._created.setdefault(call(model, "GetTitle"), set()).add(feature_name)
        call(model, "ClearSelection2", True)
        return feature_name

    # --- part modelling -------------------------------------------------------

    def create_sketch(
        self,
        doc: str | None,
        plane: str | None,
        face_point: list[float] | None,
        entities: list[dict],
        name: str | None,
        model_coordinates: bool = False,
    ) -> dict:
        model = self._part(doc)
        if not entities:
            raise SolidWorksError("A sketch needs at least one entity.")
        call(model, "ClearSelection2", True)
        if face_point is not None:
            self._select_entity(model, {"type": "face", "point": face_point}, append=False, mark=0)
        else:
            self._select_plane(model, plane or "front")

        skm = get(model, "SketchManager")
        call(skm, "InsertSketch", True)
        if get(skm, "ActiveSketch") is None:
            raise SolidWorksError("SolidWorks did not open a sketch on the selected plane/face.")
        put(skm, "AddToDB", True)
        off_plane = [0.0]
        try:
            to_sketch = self._model_to_sketch(get(skm, "ActiveSketch"), off_plane) if model_coordinates else None
            try:
                primitives = expand_entities(entities, to_sketch)
            except (ValueError, KeyError) as exc:
                raise SolidWorksError(f"Invalid sketch entities: {exc}") from exc
            for i, primitive in enumerate(primitives):
                if not self._draw(skm, primitive):
                    raise SolidWorksError(f"Sketch segment {i} ({primitive['type']}) could not be created.")
        except Exception:
            put(skm, "AddToDB", False)
            call(skm, "InsertSketch", True)
            call(model, "EditUndo2", 1)
            raise
        put(skm, "AddToDB", False)
        call(skm, "InsertSketch", True)
        sketch_name = self._record(model, None, name)
        result = {"sketch": sketch_name, "entities": len(entities), "segments": len(primitives)}
        if model_coordinates and off_plane[0] > 0.01:
            result["warning"] = (
                f"Some points were up to {off_plane[0]:.3f} mm off the sketch plane and were projected onto it."
            )
        return result

    def _model_to_sketch(self, sketch: Any, off_plane: list[float]):
        """Return a mapper from model-space mm [x, y, z] to sketch-space mm (x, y)."""
        transform = get(sketch, "ModelToSketchTransform")
        math_util = call(self._app, "GetMathUtility")

        def to_sketch(p: list[float]) -> tuple[float, float]:
            if len(p) != 3:
                raise ValueError(f"With model_coordinates=true every point must be [x, y, z] in mm, got {p}.")
            point = call(math_util, "CreatePoint", double_array(_point3(p)))
            mapped = as_list(get(call(point, "MultiplyTransform", transform), "ArrayData"))
            off_plane[0] = max(off_plane[0], abs(_mm(mapped[2])))
            return _mm(mapped[0]), _mm(mapped[1])

        return to_sketch

    def _draw(self, skm: Any, e: dict) -> bool:
        """Draw one primitive from sketch_geometry.expand_entities (sketch-space mm)."""
        kind = e.get("type")
        if kind in ("line", "centerline"):
            (x1, y1), (x2, y2) = _point2(e["start"]), _point2(e["end"])
            method = "CreateLine" if kind == "line" else "CreateCenterLine"
            return call(skm, method, x1, y1, 0.0, x2, y2, 0.0) is not None
        if kind == "polyline":
            points = [_point2(p) for p in e["points"]]
            if e.get("closed", True):
                points.append(points[0])
            for (x1, y1), (x2, y2) in pairwise(points):
                if call(skm, "CreateLine", x1, y1, 0.0, x2, y2, 0.0) is None:
                    return False
            return True
        if kind == "circle":
            cx, cy = _point2(e["center"])
            return call(skm, "CreateCircleByRadius", cx, cy, 0.0, _m(e["radius"])) is not None
        if kind == "arc":
            (cx, cy), (sx, sy), (ex, ey) = _point2(e["center"]), _point2(e["start"]), _point2(e["end"])
            direction = -1 if e.get("clockwise") else 1
            return call(skm, "CreateArc", cx, cy, 0.0, sx, sy, 0.0, ex, ey, 0.0, direction) is not None
        raise SolidWorksError(f"Unknown sketch entity type '{kind}'.")

    def extrude(
        self,
        doc: str | None,
        sketch: str,
        depth: float,
        end_condition: str,
        reverse: bool,
        second_depth: float | None,
        draft_angle: float,
        cut: bool,
        name: str | None,
        start_offset: float | None = None,
    ) -> dict:
        model = self._part(doc)
        call(model, "ClearSelection2", True)
        self._select_entity(model, {"type": "sketch", "name": sketch}, append=False, mark=0)

        single = True
        t2, d2 = c.END_BLIND, 0.0
        if end_condition == "through_all_both":
            t1, single, t2 = c.END_THROUGH_ALL, False, c.END_THROUGH_ALL
        elif end_condition in c.END_CONDITIONS:
            t1 = c.END_CONDITIONS[end_condition]
        else:
            raise SolidWorksError(f"Unknown end condition '{end_condition}'.")
        if second_depth and end_condition == "blind":
            single, d2 = False, _m(second_depth)
        d1 = _m(depth) if depth else 0.0
        draft = math.radians(draft_angle or 0.0)
        use_draft = bool(draft_angle)
        # Start condition: from the sketch plane, or offset from it (negative = opposite side).
        t0 = c.START_OFFSET if start_offset else c.START_SKETCH_PLANE
        offset = _m(abs(start_offset or 0.0))
        flip_offset = bool(start_offset and start_offset < 0)

        fm = get(model, "FeatureManager")
        if cut:
            feature = call(
                fm, "FeatureCut4",
                single, False, reverse, t1, t2, d1, d2,
                use_draft, use_draft and not single, False, False, draft, draft,
                False, False, False, False,
                False, True, True, False, False, False,
                t0, offset, flip_offset, False,
            )
        else:
            feature = call(
                fm, "FeatureExtrusion2",
                single, False, reverse, t1, t2, d1, d2,
                use_draft, use_draft and not single, False, False, draft, draft,
                False, False, False, False,
                True, True, True,
                t0, offset, flip_offset,
            )
        if feature is None:
            what = "Cut" if cut else "Extrude"
            hint = " Check the sketch overlaps the existing body." if cut else " Check the sketch profile is closed."
            raise SolidWorksError(f"{what} of '{sketch}' failed.{hint}")
        return {"feature": self._record(model, feature, name)}

    def revolve(self, doc: str | None, sketch: str, angle: float, reverse: bool, cut: bool, name: str | None) -> dict:
        model = self._part(doc)
        call(model, "ClearSelection2", True)
        self._select_entity(model, {"type": "sketch", "name": sketch}, append=False, mark=0)
        fm = get(model, "FeatureManager")
        feature = call(
            fm, "FeatureRevolve2",
            True, True, False, cut, reverse, False,
            0, 0, math.radians(angle), 0.0,
            False, False, 0.0, 0.0,
            0, 0.0, 0.0,
            True, True, True,
        )
        if feature is None:
            raise SolidWorksError(
                f"Revolve of '{sketch}' failed. The sketch needs exactly one centerline (the axis) and a closed "
                "profile on one side of it."
            )
        return {"feature": self._record(model, feature, name)}

    def create_plane(self, doc: str | None, base: str, offset: float, reverse: bool, name: str | None) -> dict:
        model = self._part(doc)
        call(model, "ClearSelection2", True)
        self._select_plane(model, base)
        constraint = c.REF_PLANE_DISTANCE | (256 if reverse else 0)
        plane = call(get(model, "FeatureManager"), "InsertRefPlane", constraint, _m(offset), 0, 0.0, 0, 0.0)
        if plane is None:
            raise SolidWorksError(f"Could not create a plane {offset} mm from '{base}'.")
        return {"plane": self._record(model, None, name)}

    def fillet(self, doc: str | None, radius: float, edges: str | list[list[float]], name: str | None) -> dict:
        model = self._part(doc)
        count = self._select_edges(model, edges)
        fm = get(model, "FeatureManager")
        r = _m(radius)
        try:
            feature = call(
                fm, "FeatureFillet3",
                c.FILLET_DEFAULT_OPTIONS, r, 0.0, 0.0, 0, 0, 0,
                empty(), empty(), empty(), empty(), empty(), empty(), empty(),
            )
        except SolidWorksError:
            # Releases before FeatureFillet3 (pre-2016 SP-level differences).
            feature = call(fm, "FeatureFillet", c.FILLET_DEFAULT_OPTIONS, r, 0, 0, empty(), empty(), empty())
        if feature is None:
            raise SolidWorksError(f"Fillet of {count} edge(s) with radius {radius} mm failed. Try a smaller radius.")
        return {"feature": self._record(model, feature, name), "edges": count}

    def chamfer(
        self, doc: str | None, distance: float, angle: float, edges: str | list[list[float]], name: str | None
    ) -> dict:
        model = self._part(doc)
        count = self._select_edges(model, edges)
        feature = call(
            get(model, "FeatureManager"), "InsertFeatureChamfer",
            1, c.CHAMFER_ANGLE_DISTANCE, _m(distance), math.radians(angle), 0.0, 0.0, 0.0, 0.0,
        )
        if feature is None:
            raise SolidWorksError(f"Chamfer of {count} edge(s) failed. Try a smaller distance.")
        return {"feature": self._record(model, feature, name), "edges": count}

    def shell(
        self, doc: str | None, thickness: float, remove_face_points: list[list[float]], outward: bool, name: str | None
    ) -> dict:
        model = self._part(doc)
        call(model, "ClearSelection2", True)
        for i, point in enumerate(remove_face_points):
            self._select_entity(model, {"type": "face", "point": point}, append=i > 0, mark=0)
        call(get(model, "FeatureManager"), "InsertFeatureShell", _m(thickness), outward)
        feature = call(model, "FeatureByPositionReverse", 0)
        if feature is None or call(feature, "GetTypeName2") != "Shell":
            raise SolidWorksError("Shell failed. Try a smaller thickness or different faces.")
        return {"feature": self._record(model, feature, name)}

    def set_material(self, doc: str | None, material: str, database: str) -> dict:
        model = self._part(doc)
        config = get(call(get(model, "ConfigurationManager"), "ActiveConfiguration"), "Name")
        call(model, "SetMaterialPropertyName2", config, database, material)
        applied = call(model, "GetMaterialPropertyName2", config, out_str())
        if not applied or applied.lower() != material.lower():
            raise SolidWorksError(
                f"Material '{material}' was not found in '{database}'. Use the exact name shown in the "
                "SolidWorks material library, e.g. 'AISI 1020', '6061-T6 (SS)', 'ABS'."
            )
        return {"material": applied, "configuration": config}

    def delete_feature(self, doc: str | None, feature: str) -> dict:
        model = self._part(doc)
        title = call(model, "GetTitle")
        if feature not in self._created.get(title, set()):
            raise SolidWorksError(
                f"'{feature}' was not created in this session, so it will not be deleted. "
                "Ask the user to remove it in SolidWorks, or use sw_set_suppression."
            )
        call(model, "ClearSelection2", True)
        call(self._feature_by_name(model, feature), "Select2", False, 0)
        if not call(get(model, "Extension"), "DeleteSelection2", 1):
            raise SolidWorksError(f"Could not delete '{feature}' (other features may depend on it).")
        self._created[title].discard(feature)
        return {"deleted": feature}

    def set_suppression(self, doc: str | None, feature: str, suppressed: bool) -> dict:
        model = self._doc(doc)
        call(model, "ClearSelection2", True)
        call(self._feature_by_name(model, feature), "Select2", False, 0)
        method = "EditSuppress2" if suppressed else "EditUnsuppress2"
        if not call(model, method):
            raise SolidWorksError(f"Could not {'suppress' if suppressed else 'unsuppress'} '{feature}'.")
        return {"feature": feature, "suppressed": suppressed}

    def set_dimension(self, doc: str | None, dimension: str, value: float) -> dict:
        model = self._doc(doc)
        dim = call(model, "Parameter", dimension)
        if dim is None:
            raise SolidWorksError(
                f"No dimension '{dimension}'. Use the full name from sw_list_features, e.g. 'D1@Boss-Extrude1'."
            )
        put(dim, "SystemValue", _m(value))
        call(model, "ForceRebuild3", False)
        return {"dimension": dimension, "value_mm": value}

    def undo(self, doc: str | None, steps: int) -> dict:
        model = self._doc(doc)
        call(model, "EditUndo2", int(steps))
        return {"undone_steps": steps}

    def mirror(self, doc: str | None, features: list[str], plane: str, name: str | None) -> dict:
        model = self._part(doc)
        if not features:
            raise SolidWorksError("List at least one feature to mirror.")
        call(model, "ClearSelection2", True)
        for feature in features:
            call(self._feature_by_name(model, feature), "Select2", True, 1)
        self._select_plane(model, plane, append=True, mark=2)
        fm = get(model, "FeatureManager")
        try:
            result = call(fm, "InsertMirrorFeature2", False, False, False, False, 0)
        except SolidWorksError:
            result = call(fm, "InsertMirrorFeature", False, False, False, False)
        if result is None:
            raise SolidWorksError(f"Mirror of {features} about '{plane}' failed.")
        return {"feature": self._record(model, result, name)}

    def hole(
        self,
        doc: str | None,
        face_point: list[float],
        positions: list[list[float]],
        diameter: float,
        depth: float | None,
        hole_type: str,
        counterbore_diameter: float | None,
        counterbore_depth: float | None,
        countersink_diameter: float | None,
        countersink_angle: float,
        name: str | None,
    ) -> dict:
        """Holes on a planar face, built from sketches and cuts (simple, counterbore, countersink)."""
        if not positions:
            raise SolidWorksError("Give at least one hole position.")
        widest = max(diameter, counterbore_diameter or 0.0, countersink_diameter or 0.0) / 2
        for position in positions:
            if math.dist(position, face_point) <= widest + 0.5:
                raise SolidWorksError(
                    "face_point lies inside a hole. Pick a point on the same face away from all holes."
                )

        def cut(radius: float, cut_depth: float | None, draft: float = 0.0, label: str = "") -> str:
            circles = [{"type": "circle", "center": p, "radius": radius} for p in positions]
            sketch = self.create_sketch(doc, None, face_point, circles, None, model_coordinates=True)["sketch"]
            end = "blind" if cut_depth else "through_all"
            feature_name = f"{name}{label}" if name else None
            return self.extrude(
                doc, sketch, cut_depth or 0.0, end, False, None, draft, True, feature_name
            )["feature"]

        features = []
        if hole_type == "counterbore":
            if not counterbore_diameter or not counterbore_depth or counterbore_diameter <= diameter:
                raise SolidWorksError("Counterbores need counterbore_diameter (> diameter) and counterbore_depth.")
            features.append(cut(counterbore_diameter / 2, counterbore_depth, label="-Counterbore"))
        elif hole_type == "countersink":
            if not countersink_diameter or countersink_diameter <= diameter:
                raise SolidWorksError("Countersinks need countersink_diameter (> diameter).")
            half = math.radians(countersink_angle / 2)
            sink_depth = (countersink_diameter - diameter) / 2 / math.tan(half)
            # Draft is measured from the cut direction, so it equals the countersink half-angle.
            features.append(cut(countersink_diameter / 2, sink_depth, countersink_angle / 2, "-Countersink"))
        elif hole_type != "simple":
            raise SolidWorksError("hole_type must be 'simple', 'counterbore' or 'countersink'.")
        features.append(cut(diameter / 2, depth, label="-Hole"))
        return {"features": features, "holes": len(positions)}

    def list_entities(
        self,
        doc: str | None,
        kind: str,
        near: list[float] | None,
        limit: int,
    ) -> dict:
        """Faces and edges with geometry and a pick point usable by other tools."""
        model = self._part(doc)
        faces, edges = [], []
        for body in as_list(call(model, "GetBodies2", 0, True)):
            if kind in ("faces", "all"):
                faces += [self._describe_face(f) for f in as_list(call(body, "GetFaces"))]
            if kind in ("edges", "all"):
                edges += [self._describe_edge(e) for e in as_list(call(body, "GetEdges"))]

        def ranked(items: list[dict]) -> list[dict]:
            items = [i for i in items if i.get("pick_point")]
            if near is not None:
                items.sort(key=lambda i: math.dist(i["pick_point"], near))
            return items[:limit]

        result: dict[str, Any] = {"document": call(model, "GetTitle")}
        if kind in ("faces", "all"):
            result["faces"] = ranked(faces)
            result["face_count"] = len(faces)
        if kind in ("edges", "all"):
            result["edges"] = ranked(edges)
            result["edge_count"] = len(edges)
        return result

    def _describe_face(self, face: Any) -> dict:
        info: dict[str, Any] = {"type": "other"}
        try:
            info["area_mm2"] = round(call(face, "GetArea") * 1e6, 3)
            surface = call(face, "GetSurface")
            if call(surface, "IsPlane"):
                info["type"] = "plane"
                info["normal"] = [round(v, 6) for v in as_list(get(face, "Normal"))[:3]]
            elif call(surface, "IsCylinder"):
                params = as_list(get(surface, "CylinderParams"))
                info.update(
                    type="cylinder",
                    axis_point=[_mm(v) for v in params[:3]],
                    axis=[round(v, 6) for v in params[3:6]],
                    diameter=round(_mm(params[6]) * 2, 6),
                )
            info["pick_point"] = self._face_point(face)
        except SolidWorksError as exc:
            info["error"] = str(exc)
        return info

    def _face_point(self, face: Any) -> list[float] | None:
        """A point strictly inside the face: centroid of its largest tessellation triangle."""
        coords = as_list(call(face, "GetTessTriangles", True))
        best, best_area = None, -1.0
        for i in range(0, len(coords) - 8, 9):
            a, b, c3 = coords[i:i + 3], coords[i + 3:i + 6], coords[i + 6:i + 9]
            ab = [b[k] - a[k] for k in range(3)]
            ac = [c3[k] - a[k] for k in range(3)]
            cross = [ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2], ab[0] * ac[1] - ab[1] * ac[0]]
            area = math.hypot(*cross)
            if area > best_area:
                best_area, best = area, [_mm((a[k] + b[k] + c3[k]) / 3) for k in range(3)]
        return best

    def _describe_edge(self, edge: Any) -> dict:
        info: dict[str, Any] = {"type": "other"}
        try:
            curve = call(edge, "GetCurve")
            params = as_list(call(edge, "GetCurveParams2"))
            start, end, t0, t1 = params[0:3], params[3:6], params[6], params[7]
            info["start"] = [_mm(v) for v in start]
            info["end"] = [_mm(v) for v in end]
            info["pick_point"] = [_mm(v) for v in as_list(call(curve, "Evaluate2", (t0 + t1) / 2, 0))[:3]]
            info["length_mm"] = round(_mm(call(curve, "GetLength3", t0, t1)), 6)
            if call(curve, "IsLine"):
                info["type"] = "line"
            elif call(curve, "IsCircle"):
                circle = as_list(get(curve, "CircleParams"))
                info.update(
                    type="circle",
                    center=[_mm(v) for v in circle[:3]],
                    axis=[round(v, 6) for v in circle[3:6]],
                    diameter=round(_mm(circle[6]) * 2, 6),
                )
        except SolidWorksError as exc:
            info["error"] = str(exc)
        return info

    # --- inspection -----------------------------------------------------------

    def list_features(self, doc: str | None, include_dimensions: bool) -> dict:
        model = self._doc(doc)
        created = self._created.get(call(model, "GetTitle"), set())
        result = []
        for feat in self._features(model):
            type_name = call(feat, "GetTypeName2")
            if type_name.endswith("Folder") or type_name in HIDDEN_FEATURE_TYPES:
                continue
            name = get(feat, "Name")
            entry = {
                "name": name,
                "type": type_name,
                "suppressed": bool(call(feat, "IsSuppressed")),
                "created_by_server": name in created,
            }
            if include_dimensions:
                entry["dimensions"] = self._feature_dimensions(feat)
            result.append(entry)
        return {"document": call(model, "GetTitle"), "features": result}

    def _feature_dimensions(self, feat: Any) -> list[dict]:
        dims = []
        display_dim = call(feat, "GetFirstDisplayDimension")
        while display_dim is not None:
            dim = call(display_dim, "GetDimension2", 0)
            if dim is not None:
                dims.append({"name": get(dim, "FullName"), "value": round(get(dim, "SystemValue"), 9)})
            display_dim = call(feat, "GetNextDisplayDimension", display_dim)
        return dims

    def rebuild(self, doc: str | None) -> dict:
        model = self._doc(doc)
        ok = bool(call(model, "ForceRebuild3", False))
        errors = []
        for feat in self._features(model):
            is_warning = out_bool()
            code = call(feat, "GetErrorCode2", is_warning)
            if code:
                errors.append({"feature": get(feat, "Name"), "code": code, "warning_only": bool(is_warning.value)})
        return {"rebuilt": ok, "problems": errors}

    def mass_properties(self, doc: str | None) -> dict:
        model = self._doc(doc)
        mp = call(get(model, "Extension"), "CreateMassProperty")
        if mp is None:
            raise SolidWorksError("No solid bodies to measure.")
        com = as_list(get(mp, "CenterOfMass"))
        result = {
            "mass_kg": round(get(mp, "Mass"), 6),
            "volume_mm3": round(get(mp, "Volume") * 1e9, 3),
            "surface_area_mm2": round(get(mp, "SurfaceArea") * 1e6, 3),
            "density_kg_m3": round(get(mp, "Density"), 3),
            "center_of_mass_mm": [_mm(v) for v in com[:3]],
        }
        if call(model, "GetType") == c.DOC_PART:
            box = as_list(call(model, "GetPartBox", True))
            if len(box) == 6:
                lo, hi = [_mm(v) for v in box[:3]], [_mm(v) for v in box[3:]]
                result["bounding_box_mm"] = {"min": lo, "max": hi, "size": [round(h - l, 6) for l, h in zip(lo, hi)]}
        return result

    def screenshot(self, doc: str | None, view: str) -> dict:
        model = self._doc(doc)
        named = c.STANDARD_VIEWS.get(view)
        if named is None:
            raise SolidWorksError(f"Unknown view '{view}'. Use one of {sorted(c.STANDARD_VIEWS)}.")
        call(model, "ShowNamedView2", named, -1)
        call(model, "ViewZoomtofit2")
        call(model, "GraphicsRedraw2")
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in (".png", ".jpg"):
                target = Path(tmp) / f"view{suffix}"
                self._save_as(model, target, copy=True)
                if target.exists():
                    image = base64.b64encode(target.read_bytes()).decode()
                    return {"format": "png" if suffix == ".png" else "jpeg", "data_base64": image}
        raise SolidWorksError("SolidWorks could not render an image of this document.")

    # --- saving & exporting -----------------------------------------------------

    def _save_as(self, model: Any, target: Path, copy: bool) -> None:
        errors, warnings = out_long(), out_long()
        options = c.SAVE_AS_SILENT | (c.SAVE_AS_COPY if copy else 0)
        ext = get(model, "Extension")
        ok = call(ext, "SaveAs", str(target), c.SAVE_AS_CURRENT_VERSION, options, nothing(), errors, warnings)
        if not ok:
            raise SolidWorksError(f"SolidWorks could not write '{target}' (error code {errors.value}).")

    def save(self, doc: str | None, path: str | None, overwrite: bool) -> dict:
        model = self._doc(doc)
        doc_type = call(model, "GetType")
        native = {v: k for k, v in c.DOC_EXTENSIONS.items()}[doc_type]
        if not path:
            path = call(model, "GetPathName") or f"{Path(call(model, 'GetTitle')).stem}{native}"
        target = resolve_write_path(path, allowed_suffixes={native}, overwrite=overwrite)
        self._save_as(model, target, copy=False)
        return {"saved": str(target), "bytes": target.stat().st_size}

    def export(self, doc: str | None, file_format: str, path: str | None, overwrite: bool) -> dict:
        model = self._doc(doc)
        suffix = c.EXPORT_FORMATS.get(file_format)
        if suffix is None:
            raise SolidWorksError(f"Unknown format '{file_format}'. Use one of {sorted(c.EXPORT_FORMATS)}.")
        if not path:
            path = f"{Path(call(model, 'GetTitle')).stem}{suffix}"
        allowed = {suffix, ".stp"} if file_format == "step" else {suffix, ".iges"} if file_format == "iges" else {suffix}
        target = resolve_write_path(path, allowed_suffixes=allowed, overwrite=overwrite)
        self._save_as(model, target, copy=True)
        return {"exported": str(target), "format": file_format, "bytes": target.stat().st_size}

    # --- assemblies ---------------------------------------------------------------

    def insert_component(self, doc: str | None, path: str, position: list[float], fixed: bool) -> dict:
        asm = self._assembly(doc)
        asm_title = call(asm, "GetTitle")
        file = Path(path).expanduser().resolve()
        doc_type = c.DOC_EXTENSIONS.get(file.suffix.lower())
        if doc_type not in (c.DOC_PART, c.DOC_ASSEMBLY) or not file.is_file():
            raise SolidWorksError(f"'{file}' is not an existing .sldprt or .sldasm file.")

        # AddComponent needs the component loaded in memory.
        call(self._app, "OpenDoc6", str(file), doc_type, c.OPEN_SILENT, "", out_long(), out_long())
        call(self._app, "ActivateDoc3", asm_title, False, 0, out_long())

        x, y, z = _point3(position)
        component = call(asm, "AddComponent5", str(file), 0, "", False, "", x, y, z)
        if component is None:
            raise SolidWorksError(f"Could not insert '{file.name}'.")
        name = get(component, "Name2")
        if fixed:
            call(asm, "ClearSelection2", True)
            call(component, "Select4", False, nothing())
            call(asm, "FixComponent")
        call(asm, "ClearSelection2", True)
        return {"component": name, "fixed": fixed}

    def add_mate(
        self,
        doc: str | None,
        mate_type: str,
        entity1: dict,
        entity2: dict,
        alignment: str,
        distance: float | None,
        angle: float | None,
        flip: bool,
    ) -> dict:
        asm = self._assembly(doc)
        if mate_type not in c.MATE_TYPES:
            raise SolidWorksError(f"Unknown mate type '{mate_type}'. Use one of {sorted(c.MATE_TYPES)}.")
        call(asm, "ClearSelection2", True)
        self._select_entity(asm, entity1, append=False, mark=1)
        self._select_entity(asm, entity2, append=True, mark=1)
        d = _m(distance or 0.0)
        a = math.radians(angle or 0.0)
        error = out_long()
        mate = call(
            asm, "AddMate5",
            c.MATE_TYPES[mate_type], c.MATE_ALIGN.get(alignment, 2), flip,
            d, d, d, 0.0, 0.0, a, a, a,
            False, False, 0, error,
        )
        call(asm, "ClearSelection2", True)
        if mate is None:
            raise SolidWorksError(
                f"{mate_type} mate failed (error code {error.value}). Check both entities are on "
                "different components and suit this mate type."
            )
        call(asm, "EditRebuild3")
        return {"mate": mate_type, "status_code": error.value}

    def list_components(self, doc: str | None) -> dict:
        asm = self._assembly(doc)
        components = []
        for comp in as_list(call(asm, "GetComponents", True)):
            transform = get(comp, "Transform2")
            origin = as_list(get(transform, "ArrayData"))[9:12] if transform is not None else []
            components.append({
                "name": get(comp, "Name2"),
                "path": call(comp, "GetPathName"),
                "configuration": get(comp, "ReferencedConfiguration"),
                "fixed": bool(call(comp, "IsFixed")),
                "suppressed": bool(call(comp, "IsSuppressed")),
                "origin_mm": [_mm(v) for v in origin],
            })
        return {"assembly": call(asm, "GetTitle"), "components": components}


# Methods the MCP server and the bridge may invoke by name. Nothing else is reachable remotely.
OPERATIONS = frozenset({
    "status", "connect", "new_document", "open_document", "close_document",
    "create_sketch", "extrude", "revolve", "create_plane", "fillet", "chamfer", "shell",
    "mirror", "hole", "list_entities",
    "set_material", "delete_feature", "set_suppression", "set_dimension", "undo",
    "list_features", "rebuild", "mass_properties", "screenshot", "save", "export",
    "insert_component", "add_mate", "list_components",
})
