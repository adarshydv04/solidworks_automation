"""End-to-end check against a real SolidWorks (Windows). Starts SolidWorks if needed.

Builds a 100 x 60 x 10 mm plate with four holes, fillets it, assigns a material,
prints mass properties and exports a STEP file to the output folder.

    uv run python examples/live_smoke.py
"""

import json

from solidworks_automation.com import ComThread
from solidworks_automation.operations import SolidWorksOps


def main() -> None:
    thread = ComThread()
    ops = thread.submit(SolidWorksOps).result()

    def step(op: str, **kwargs):
        result = thread.submit(getattr(ops, op), **kwargs).result()
        print(f"{op}: {json.dumps(result)[:300]}")
        return result

    step("connect", launch=True)
    step("new_document", kind="part")
    plate = step(
        "create_sketch", doc=None, plane="front", face_point=None, name="PlateSketch",
        entities=[{"type": "center_rectangle", "center": [0, 0], "width": 100, "height": 60}],
    )
    step(
        "extrude", doc=None, sketch=plate["sketch"], depth=10, end_condition="blind", reverse=False,
        second_depth=None, draft_angle=0, cut=False, name="Plate",
    )
    holes = step(
        "create_sketch", doc=None, plane="front", face_point=None, name="HoleSketch",
        entities=[{"type": "circle", "center": [x, y], "radius": 4} for x in (-40, 40) for y in (-20, 20)],
    )
    step(
        "extrude", doc=None, sketch=holes["sketch"], depth=0, end_condition="through_all_both", reverse=False,
        second_depth=None, draft_angle=0, cut=True, name="Holes",
    )
    step("fillet", doc=None, radius=2, edges=[[50, 30, 5], [-50, 30, 5], [50, -30, 5], [-50, -30, 5]], name=None)
    step("set_material", doc=None, material="AISI 1020", database="solidworks materials.sldmat")
    step("rebuild", doc=None)
    step("mass_properties", doc=None)
    step("list_features", doc=None, include_dimensions=True)
    step("export", doc=None, file_format="step", path="live_smoke_plate.step", overwrite=True)
    thread.shutdown()


if __name__ == "__main__":
    main()
