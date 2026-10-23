"""End-to-end check against a real SolidWorks (Windows). Starts SolidWorks if needed.

Builds a 100 x 60 x 10 mm plate and then exercises every modelling feature:
sketch patterns, slots, model-coordinate sketches on a face, counterbore and
countersink holes, mirror, offset/up-to-next extrudes, entity listing, fillets,
material, mass properties, screenshots and STEP export.

Each step reports OK or FAILED independently, so one failure doesn't hide the rest.

    uv run python examples/live_smoke.py
"""

import json
import traceback

from solidworks_automation.com import ComThread
from solidworks_automation.operations import SolidWorksOps


def main() -> None:
    thread = ComThread()
    ops = thread.submit(SolidWorksOps).result()
    failures: list[str] = []

    def step(label: str, op: str, **kwargs):
        try:
            result = thread.submit(getattr(ops, op), **kwargs).result()
        except Exception as exc:  # noqa: BLE001 - report every failure and keep going
            failures.append(label)
            print(f"FAILED {label}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=1)
            return None
        print(f"OK     {label}: {json.dumps(result)[:240]}")
        return result

    def sketch(label: str, entities: list[dict], plane: str = "front", face_point=None, model_coordinates=False):
        result = step(label, "create_sketch", doc=None, plane=plane, face_point=face_point, entities=entities,
                      name=None, model_coordinates=model_coordinates)
        return result["sketch"] if result else None

    def extrude(label: str, sketch_name, depth: float, end: str = "blind", cut: bool = False, **extra):
        if sketch_name:
            step(label, "extrude", doc=None, sketch=sketch_name, depth=depth, end_condition=end, reverse=False,
                 second_depth=None, draft_angle=0, cut=cut, name=None, **extra)

    step("connect", "connect", launch=True)
    step("new part", "new_document", kind="part")

    # Base plate 100 x 60 x 10 (front plane, extruded +Z from 0 to 10).
    base = sketch("base sketch", [{"type": "center_rectangle", "center": [0, 0], "width": 100, "height": 60}])
    extrude("base extrude", base, 10)

    # Corner holes via a linear grid pattern.
    holes = sketch("hole grid sketch", [{
        "type": "circle", "center": [-40, -20], "radius": 3,
        "pattern": {"type": "linear", "count": 2, "spacing": [80, 0], "count2": 2, "spacing2": [0, 40]},
    }])
    extrude("hole grid cut", holes, 0, end="through_all_both", cut=True)

    # Slot cut through the middle.
    slot = sketch("slot sketch", [{"type": "slot", "center1": [-15, 0], "center2": [15, 0], "width": 8}])
    extrude("slot cut", slot, 0, end="through_all_both", cut=True)

    # Boss on the top face (z = 10) using model coordinates.
    boss = sketch(
        "boss sketch on face (model coords)",
        [{"type": "circle", "center": [30, 15, 10], "radius": 6}],
        face_point=[0, 20, 10], model_coordinates=True,
    )
    extrude("boss extrude", boss, 8)
    step("mirror boss about right plane", "mirror", doc=None, features=["Boss-Extrude2"], plane="right", name=None)

    # Bolt circle (circular pattern) of small blind holes on the top face.
    bolt = sketch(
        "bolt circle sketch (model coords)",
        [{"type": "circle", "center": [0, -12, 10], "radius": 1.5,
          "pattern": {"type": "circular", "center": [0, -20, 10], "count": 6}}],
        face_point=[0, 20, 10], model_coordinates=True,
    )
    extrude("bolt circle cut", bolt, 4, cut=True)

    # Counterbore and countersink holes on the top face.
    step("counterbore holes", "hole", doc=None, face_point=[0, 20, 10], positions=[[-40, 0, 10]], diameter=6,
         depth=None, hole_type="counterbore", counterbore_diameter=11, counterbore_depth=4,
         countersink_diameter=None, countersink_angle=90, name=None)
    step("countersink holes", "hole", doc=None, face_point=[0, 20, 10], positions=[[40, 0, 10]], diameter=6,
         depth=None, hole_type="countersink", counterbore_diameter=None, counterbore_depth=None,
         countersink_diameter=12, countersink_angle=90, name=None)

    # Rib that starts 10 mm off the front plane (on top of the plate).
    rib = sketch("rib sketch", [{"type": "center_rectangle", "center": [0, 25], "width": 60, "height": 4}])
    extrude("rib extrude (start offset)", rib, 5, start_offset=10)

    entities = step("list entities near a corner", "list_entities", doc=None, kind="edges", near=[50, 30, 5], limit=4)
    if entities and entities.get("edges"):
        corner = entities["edges"][0]["pick_point"]
        step("fillet nearest edge", "fillet", doc=None, radius=2, edges=[corner], name=None)

    step("material", "set_material", doc=None, material="6061-T6 (SS)", database="solidworks materials.sldmat")
    step("rebuild", "rebuild", doc=None)
    step("mass properties", "mass_properties", doc=None)
    for view in ("isometric", "front", "top"):
        step(f"screenshot {view}", "screenshot", doc=None, view=view)
    step("export STEP", "export", doc=None, file_format="step", path="live_smoke_plate.step", overwrite=True)

    thread.shutdown()
    print()
    print("All steps passed." if not failures else f"{len(failures)} step(s) failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
