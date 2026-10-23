"""SolidWorks API enum values used by the operations layer.

Values come from the SolidWorks API type library (swconst). They are stable
across releases, which is why we hard-code them instead of importing the
interop assemblies.
"""

# swDocumentTypes_e
DOC_PART = 1
DOC_ASSEMBLY = 2
DOC_DRAWING = 3
DOC_TYPES = {DOC_PART: "part", DOC_ASSEMBLY: "assembly", DOC_DRAWING: "drawing"}
DOC_EXTENSIONS = {".sldprt": DOC_PART, ".sldasm": DOC_ASSEMBLY, ".slddrw": DOC_DRAWING}

# swUserPreferenceStringValue_e
PREF_DEFAULT_TEMPLATE_PART = 8
PREF_DEFAULT_TEMPLATE_ASSEMBLY = 9

TEMPLATE_SUFFIXES = {"part": ".prtdot", "assembly": ".asmdot"}

# swOpenDocOptions_e
OPEN_SILENT = 1
OPEN_READ_ONLY = 2

# swSaveAsVersion_e / swSaveAsOptions_e
SAVE_AS_CURRENT_VERSION = 0
SAVE_AS_SILENT = 1
SAVE_AS_COPY = 2

# swEndConditions_e
END_BLIND = 0
END_THROUGH_ALL = 1
END_MID_PLANE = 6
END_UP_TO_NEXT = 2
END_CONDITIONS = {
    "blind": END_BLIND,
    "through_all": END_THROUGH_ALL,
    "up_to_next": END_UP_TO_NEXT,
    "mid_plane": END_MID_PLANE,
}

# swStartConditions_e
START_SKETCH_PLANE = 0
START_OFFSET = 3

# swRefPlaneReferenceConstraints_e
REF_PLANE_DISTANCE = 8

# swChamferType_e
CHAMFER_ANGLE_DISTANCE = 1

# swFeatureFilletOptions_e: UniformRadius | PropagateToTangentFaces | ... (common default)
FILLET_DEFAULT_OPTIONS = 195

# swMateType_e
MATE_TYPES = {
    "coincident": 0,
    "concentric": 1,
    "perpendicular": 2,
    "parallel": 3,
    "tangent": 4,
    "distance": 5,
    "angle": 6,
}

# swMateAlign_e
MATE_ALIGN = {"aligned": 0, "anti_aligned": 1, "closest": 2}

# swStandardViews_e names accepted by ShowNamedView2
STANDARD_VIEWS = {
    "front": "*Front",
    "back": "*Back",
    "left": "*Left",
    "right": "*Right",
    "top": "*Top",
    "bottom": "*Bottom",
    "isometric": "*Isometric",
    "trimetric": "*Trimetric",
    "dimetric": "*Dimetric",
}

# The three default reference planes, in feature-tree order. Selected by
# position rather than name so localized SolidWorks installs work too.
STANDARD_PLANES = ("front", "top", "right")

# File types sw_export may write (SaveAs picks the translator from the extension).
EXPORT_FORMATS = {
    "step": ".step",
    "iges": ".igs",
    "stl": ".stl",
    "parasolid": ".x_t",
    "3mf": ".3mf",
    "pdf": ".pdf",
    "dxf": ".dxf",
    "dwg": ".dwg",
    "png": ".png",
    "jpg": ".jpg",
}

MM = 0.001  # SolidWorks API works in metres; all tool inputs are millimetres.
