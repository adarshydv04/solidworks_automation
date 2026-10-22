"""MCP server exposing SolidWorks design tools to any MCP-capable agent."""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, Image, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ClientCapabilities, ElicitationCapability, ToolAnnotations
from pydantic import BaseModel, Field

from .backend import Backend, backend_from_env
from .com import SolidWorksError
from .safety import PolicyError

INSTRUCTIONS = """\
Drive a SolidWorks session on the user's computer.

Workflow: call sw_status first. If SolidWorks is not running, call sw_connect - the user is asked
for permission before SolidWorks is started. Then sw_new_document (or sw_open), build features,
check the result with sw_screenshot / sw_mass_properties, and sw_save or sw_export.

Units and coordinates: all lengths are millimetres, all angles degrees. Model space is Y-up with
the Front plane at Z=0. Sketch entities use 2D sketch coordinates [x, y]:
  front plane: sketch x = model X, sketch y = model Y   (normal +Z)
  top plane:   sketch x = model X, sketch y = -model Z  (normal +Y)
  right plane: sketch x = -model Z, sketch y = model Y  (normal +X)
Prefer sketching on standard or offset planes; sketches on faces use the face's own sketch axes.

Typical part: sketch on 'front' -> sw_extrude -> sketch holes on 'front' -> sw_cut_extrude with
end_condition 'through_all_both' -> sw_fillet. Feature changes happen in memory; nothing is
written to disk until sw_save or sw_export.
"""

mcp = MCPServer("solidworks", instructions=INSTRUCTIONS)

_backend: Backend | None = None

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
EDITS = ToolAnnotations(read_only_hint=False, destructive_hint=False)
WRITES_FILES = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = backend_from_env()
    return _backend


def set_backend(value: Backend | None) -> None:
    """Swap the backend (used by tests)."""
    global _backend
    _backend = value


async def run(op: str, **kwargs: Any) -> dict:
    try:
        return await backend().call(op, **kwargs)
    except (SolidWorksError, PolicyError) as exc:
        raise ToolError(str(exc)) from exc


# --- user permission prompts ----------------------------------------------------
#
# Each resolver below runs before its tool body. Returning ``Elicit(...)`` shows the user a
# permission prompt (over whichever MCP protocol version the client speaks); returning
# ``NO_PROMPT`` means no question was asked, either because none is needed or because the
# client cannot display prompts.

NO_PROMPT = "no_prompt"


class Approval(BaseModel):
    approve: bool = Field(description="Allow this action?")


Permission = ElicitationResult[Approval]


def _can_prompt(ctx: Context) -> bool:
    session = ctx.request_context.session
    return session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))


def user_answer(outcome: Permission) -> bool | None:
    """True/False for an answered prompt, None when no prompt was shown."""
    if isinstance(outcome, AcceptedElicitation):
        return outcome.data.approve if isinstance(outcome.data, Approval) else None
    return False  # declined or dismissed


async def _start_permission(ctx: Context) -> Elicit[Approval] | str:
    status = await run("connect", launch=False)
    if status["connected"] or not status.get("installed") or not _can_prompt(ctx):
        return NO_PROMPT
    return Elicit(
        f"An AI assistant wants to start SolidWorks on your computer ({backend().description}) "
        "to create or edit designs. Allow it?",
        Approval,
    )


async def _discard_permission(ctx: Context, doc: str | None, discard_changes: bool) -> Elicit[Approval] | str:
    if not discard_changes or not _can_prompt(ctx):
        return NO_PROMPT
    return Elicit(f"Close '{doc or 'the active document'}' and discard its unsaved changes?", Approval)


async def _overwrite_permission(ctx: Context, path: str | None, overwrite: bool) -> Elicit[Approval] | str:
    if not overwrite or not _can_prompt(ctx):
        return NO_PROMPT
    return Elicit(f"Replace the existing file '{path or 'of the active document'}' if it exists?", Approval)


Doc = Annotated[str | None, Field(description="Document title or path. Defaults to the active document.")]
Point2 = Annotated[list[float], Field(min_length=2, max_length=2, description="[x, y] in sketch mm")]
Point3 = Annotated[list[float], Field(min_length=3, max_length=3, description="[x, y, z] in model mm")]
FeatureName = Annotated[str | None, Field(description="Optional name for the new feature.")]
Edges = Annotated[
    Literal["all"] | list[Point3],
    Field(description="'all' for every edge of the part, or a list of model points lying on the edges."),
]


# --- sketch entity schema -------------------------------------------------------


class Line(BaseModel):
    type: Literal["line"]
    start: Point2
    end: Point2


class Centerline(BaseModel):
    """Construction line; used as the axis for sw_revolve."""

    type: Literal["centerline"]
    start: Point2
    end: Point2


class Polyline(BaseModel):
    type: Literal["polyline"]
    points: list[Point2] = Field(min_length=2)
    closed: bool = True


class Rectangle(BaseModel):
    type: Literal["rectangle"]
    corner1: Point2
    corner2: Point2


class CenterRectangle(BaseModel):
    type: Literal["center_rectangle"]
    center: Point2
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class Circle(BaseModel):
    type: Literal["circle"]
    center: Point2
    radius: float = Field(gt=0)


class Arc(BaseModel):
    """Arc from start to end around center (counter-clockwise unless clockwise=true)."""

    type: Literal["arc"]
    center: Point2
    start: Point2
    end: Point2
    clockwise: bool = False


class Polygon(BaseModel):
    type: Literal["polygon"]
    center: Point2
    radius: float = Field(gt=0, description="Distance from center to a vertex (inscribed) or edge midpoint.")
    sides: int = Field(ge=3, le=40)
    inscribed: bool = True


SketchEntity = Annotated[
    Line | Centerline | Polyline | Rectangle | CenterRectangle | Circle | Arc | Polygon,
    Field(discriminator="type"),
]


class MateEntity(BaseModel):
    """An entity to mate: a face/edge/vertex at a model point, or a plane/axis by name."""

    type: Literal["face", "edge", "vertex", "plane", "axis"] = "face"
    point: Point3 | None = None
    name: str | None = Field(
        default=None, description="Full SolidWorks name, e.g. 'Front Plane@Bracket-1@Assem1'."
    )


# --- session ------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def sw_status() -> dict:
    """Report whether SolidWorks is installed and running, its release, and the open documents.

    Call this first.
    """
    return await run("status")


@mcp.tool(annotations=EDITS)
async def sw_connect(
    ctx: Context,
    permission: Annotated[Permission, Resolve(_start_permission)],
    user_approved: Annotated[
        bool,
        Field(
            description="Only for clients that cannot show permission prompts: set true ONLY after the user "
            "has explicitly agreed in the conversation to start SolidWorks."
        ),
    ] = False,
) -> dict:
    """Connect to SolidWorks, starting it if needed - only with the user's permission.

    If SolidWorks is already running, this attaches to it. Otherwise the user is shown a prompt
    asking whether SolidWorks may be started on their computer.
    """
    status = await run("connect", launch=False)
    if status["connected"]:
        return status
    if not status.get("installed"):
        raise ToolError("SolidWorks is not installed on the target Windows machine.")

    approved = user_answer(permission)
    if approved is None:
        approved = user_approved
        if not approved:
            raise ToolError(
                "SolidWorks is not running and this client cannot show a permission prompt. Ask the user whether "
                "SolidWorks may be started; if they agree, call sw_connect again with user_approved=true."
            )
    if not approved:
        raise ToolError("The user declined starting SolidWorks. Do not retry unless they ask.")
    await ctx.report_progress(0, 1, "Starting SolidWorks - this can take a minute or two")
    return await run("connect", launch=True)


# --- documents ----------------------------------------------------------------


@mcp.tool(annotations=EDITS)
async def sw_new_document(kind: Literal["part", "assembly"] = "part") -> dict:
    """Create a new, empty part or assembly from the user's default template and make it active."""
    return await run("new_document", kind=kind)


@mcp.tool(annotations=EDITS)
async def sw_open(
    path: Annotated[str, Field(description="Full path of a .sldprt, .sldasm or .slddrw file.")],
    read_only: bool = False,
) -> dict:
    """Open an existing SolidWorks document and make it active."""
    return await run("open_document", path=path, read_only=read_only)


@mcp.tool(annotations=WRITES_FILES)
async def sw_close(
    permission: Annotated[Permission, Resolve(_discard_permission)],
    doc: Doc = None,
    discard_changes: bool = False,
) -> dict:
    """Close a document. Refuses if there are unsaved changes unless discard_changes=true."""
    if user_answer(permission) is False:
        raise ToolError("The user chose not to discard the changes.")
    return await run("close_document", doc=doc, discard_changes=discard_changes)


# --- part modelling -------------------------------------------------------------


@mcp.tool(annotations=EDITS)
async def sw_create_sketch(
    entities: Annotated[list[SketchEntity], Field(min_length=1)],
    plane: Annotated[
        str,
        Field(description="'front', 'top', 'right', or the name of a reference plane such as 'Plane1'."),
    ] = "front",
    face_point: Annotated[
        Point3 | None,
        Field(description="Sketch on the planar face at this model point instead of on `plane`."),
    ] = None,
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Create a 2D sketch made of lines, rectangles, circles, arcs, polygons and centerlines.

    Coordinates are sketch-plane millimetres (see server instructions for plane axes). Closed
    profiles can then be used by sw_extrude, sw_cut_extrude or sw_revolve. Returns the sketch name.
    """
    return await run(
        "create_sketch",
        doc=doc,
        plane=plane,
        face_point=face_point,
        entities=[e.model_dump() for e in entities],
        name=name,
    )


EndCondition = Literal["blind", "through_all", "through_all_both", "mid_plane"]


@mcp.tool(annotations=EDITS)
async def sw_extrude(
    sketch: Annotated[str, Field(description="Sketch name, e.g. 'Sketch1'.")],
    depth: Annotated[float, Field(gt=0, description="Depth in mm (total depth for mid_plane).")],
    end_condition: Literal["blind", "mid_plane"] = "blind",
    reverse: Annotated[bool, Field(description="Extrude in the opposite direction to the plane normal.")] = False,
    second_depth: Annotated[float | None, Field(gt=0, description="Also extrude this far the other way.")] = None,
    draft_angle: Annotated[float, Field(ge=0, lt=89, description="Draft angle in degrees.")] = 0,
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Extrude a closed sketch profile into a solid (boss/base extrude)."""
    return await run(
        "extrude", doc=doc, sketch=sketch, depth=depth, end_condition=end_condition, reverse=reverse,
        second_depth=second_depth, draft_angle=draft_angle, cut=False, name=name,
    )


@mcp.tool(annotations=EDITS)
async def sw_cut_extrude(
    sketch: Annotated[str, Field(description="Sketch name, e.g. 'Sketch2'.")],
    end_condition: Annotated[
        EndCondition,
        Field(description="'through_all_both' cuts through the whole part in both directions - safest for holes."),
    ] = "through_all_both",
    depth: Annotated[float | None, Field(gt=0, description="Cut depth in mm (blind / mid_plane).")] = None,
    reverse: bool = False,
    draft_angle: Annotated[float, Field(ge=0, lt=89)] = 0,
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Remove material by extruding a sketch profile through the part (holes, pockets, slots)."""
    if end_condition in ("blind", "mid_plane") and not depth:
        raise ToolError("depth is required for blind and mid_plane cuts.")
    return await run(
        "extrude", doc=doc, sketch=sketch, depth=depth or 0.0, end_condition=end_condition, reverse=reverse,
        second_depth=None, draft_angle=draft_angle, cut=True, name=name,
    )


@mcp.tool(annotations=EDITS)
async def sw_revolve(
    sketch: Annotated[str, Field(description="Sketch with one centerline (the axis) and a closed profile.")],
    angle: Annotated[float, Field(gt=0, le=360, description="Revolve angle in degrees.")] = 360,
    reverse: bool = False,
    cut: Annotated[bool, Field(description="Remove material instead of adding it.")] = False,
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Revolve a sketch profile around its centerline (shafts, discs, bottles, grooves)."""
    return await run("revolve", doc=doc, sketch=sketch, angle=angle, reverse=reverse, cut=cut, name=name)


@mcp.tool(annotations=EDITS)
async def sw_create_plane(
    offset: Annotated[float, Field(gt=0, description="Distance in mm from the base plane.")],
    base: Annotated[str, Field(description="'front', 'top', 'right' or a plane name.")] = "front",
    reverse: Annotated[bool, Field(description="Offset to the other side of the base plane.")] = False,
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Create a reference plane parallel to a base plane, for sketching at a height/depth."""
    return await run("create_plane", doc=doc, base=base, offset=offset, reverse=reverse, name=name)


@mcp.tool(annotations=EDITS)
async def sw_fillet(
    radius: Annotated[float, Field(gt=0, description="Radius in mm.")],
    edges: Edges = "all",
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Round edges with a constant-radius fillet."""
    return await run("fillet", doc=doc, radius=radius, edges=edges, name=name)


@mcp.tool(annotations=EDITS)
async def sw_chamfer(
    distance: Annotated[float, Field(gt=0, description="Chamfer distance in mm.")],
    angle: Annotated[float, Field(gt=0, lt=90)] = 45,
    edges: Edges = "all",
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Bevel edges with an angle-distance chamfer."""
    return await run("chamfer", doc=doc, distance=distance, angle=angle, edges=edges, name=name)


@mcp.tool(annotations=EDITS)
async def sw_shell(
    thickness: Annotated[float, Field(gt=0, description="Wall thickness in mm.")],
    remove_face_points: Annotated[
        list[Point3], Field(description="Model points on the faces to remove (open faces). May be empty.")
    ],
    outward: bool = False,
    name: FeatureName = None,
    doc: Doc = None,
) -> dict:
    """Hollow out the part, leaving walls of the given thickness."""
    return await run(
        "shell", doc=doc, thickness=thickness, remove_face_points=remove_face_points, outward=outward, name=name
    )


@mcp.tool(annotations=EDITS)
async def sw_set_material(
    material: Annotated[str, Field(description="Exact library name, e.g. 'AISI 1020', '6061-T6 (SS)', 'ABS'.")],
    database: Annotated[str, Field(description="Material library file.")] = "solidworks materials.sldmat",
    doc: Doc = None,
) -> dict:
    """Assign a material to the part (affects mass properties)."""
    return await run("set_material", doc=doc, material=material, database=database)


@mcp.tool(annotations=EDITS)
async def sw_set_dimension(
    dimension: Annotated[str, Field(description="Full dimension name, e.g. 'D1@Boss-Extrude1'.")],
    value: Annotated[float, Field(description="New value in mm.")],
    doc: Doc = None,
) -> dict:
    """Change a linear dimension and rebuild. Find names with sw_list_features(include_dimensions=true)."""
    return await run("set_dimension", doc=doc, dimension=dimension, value=value)


@mcp.tool(annotations=EDITS)
async def sw_set_suppression(feature: str, suppressed: bool = True, doc: Doc = None) -> dict:
    """Suppress or unsuppress a feature."""
    return await run("set_suppression", doc=doc, feature=feature, suppressed=suppressed)


@mcp.tool(annotations=EDITS)
async def sw_delete_feature(feature: str, doc: Doc = None) -> dict:
    """Delete a feature this server created in the current session (others can only be suppressed)."""
    return await run("delete_feature", doc=doc, feature=feature)


@mcp.tool(annotations=EDITS)
async def sw_undo(steps: Annotated[int, Field(ge=1, le=20)] = 1, doc: Doc = None) -> dict:
    """Undo the last operation(s) in the document."""
    return await run("undo", doc=doc, steps=steps)


# --- inspection -----------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def sw_list_features(include_dimensions: bool = False, doc: Doc = None) -> dict:
    """List the feature tree (names, types, suppression) and optionally each feature's dimensions.

    Dimension values are in metres (SolidWorks system units).
    """
    return await run("list_features", doc=doc, include_dimensions=include_dimensions)


@mcp.tool(annotations=READ_ONLY)
async def sw_rebuild(doc: Doc = None) -> dict:
    """Rebuild the model and report features with errors or warnings."""
    return await run("rebuild", doc=doc)


@mcp.tool(annotations=READ_ONLY)
async def sw_mass_properties(doc: Doc = None) -> dict:
    """Mass, volume, surface area, centre of mass and (for parts) bounding box, in mm / kg."""
    return await run("mass_properties", doc=doc)


@mcp.tool(annotations=READ_ONLY)
async def sw_screenshot(
    view: Literal[
        "isometric", "trimetric", "dimetric", "front", "back", "left", "right", "top", "bottom"
    ] = "isometric",
    doc: Doc = None,
) -> Image:
    """Show the model from a standard view as an image, to check the design visually."""
    result = await run("screenshot", doc=doc, view=view)
    return Image(data=base64.b64decode(result["data_base64"]), format=result["format"])


# --- files ------------------------------------------------------------------------


@mcp.tool(annotations=WRITES_FILES)
async def sw_save(
    permission: Annotated[Permission, Resolve(_overwrite_permission)],
    path: Annotated[
        str | None,
        Field(description="File name (saved in the output folder) or full path in an allowed folder."),
    ] = None,
    overwrite: bool = False,
    doc: Doc = None,
) -> dict:
    """Save the document as a native SolidWorks file (.sldprt / .sldasm)."""
    if user_answer(permission) is False:
            raise ToolError("The user chose not to overwrite the file.")
    return await run("save", doc=doc, path=path, overwrite=overwrite)


@mcp.tool(annotations=WRITES_FILES)
async def sw_export(
    permission: Annotated[Permission, Resolve(_overwrite_permission)],
    file_format: Literal["step", "iges", "stl", "parasolid", "3mf", "pdf", "dxf", "dwg", "png", "jpg"],
    path: Annotated[str | None, Field(description="File name or full path in an allowed folder.")] = None,
    overwrite: bool = False,
    doc: Doc = None,
) -> dict:
    """Export a copy of the document (STEP, IGES, STL, Parasolid, 3MF, PDF, DXF/DWG, images)."""
    if user_answer(permission) is False:
        raise ToolError("The user chose not to overwrite the file.")
    return await run("export", doc=doc, file_format=file_format, path=path, overwrite=overwrite)


# --- assemblies ---------------------------------------------------------------------


@mcp.tool(annotations=EDITS)
async def sw_insert_component(
    path: Annotated[str, Field(description="Full path of a saved .sldprt or .sldasm.")],
    position: Annotated[Point3 | None, Field(description="Where to place it; defaults to the origin.")] = None,
    fixed: Annotated[bool, Field(description="Fix the component in place (usually the first one).")] = False,
    doc: Doc = None,
) -> dict:
    """Insert a saved part or sub-assembly into the active assembly."""
    return await run("insert_component", doc=doc, path=path, position=position or [0.0, 0.0, 0.0], fixed=fixed)


@mcp.tool(annotations=EDITS)
async def sw_add_mate(
    mate_type: Literal["coincident", "concentric", "parallel", "perpendicular", "tangent", "distance", "angle"],
    entity1: MateEntity,
    entity2: MateEntity,
    alignment: Literal["aligned", "anti_aligned", "closest"] = "closest",
    distance: Annotated[float | None, Field(description="mm, for distance mates.")] = None,
    angle: Annotated[float | None, Field(description="Degrees, for angle mates.")] = None,
    flip: bool = False,
    doc: Doc = None,
) -> dict:
    """Mate two components in the active assembly."""
    return await run(
        "add_mate", doc=doc, mate_type=mate_type, entity1=entity1.model_dump(), entity2=entity2.model_dump(),
        alignment=alignment, distance=distance, angle=angle, flip=flip,
    )


@mcp.tool(annotations=READ_ONLY)
async def sw_list_components(doc: Doc = None) -> dict:
    """List the top-level components of an assembly with their position and state."""
    return await run("list_components", doc=doc)
