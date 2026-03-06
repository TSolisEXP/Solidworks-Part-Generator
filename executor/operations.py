"""
SolidWorks operation executor.

Implements each OperationType as a call into the SolidWorks COM API.
All API constants are documented inline — refer to the SolidWorks API Help
(swconst.h / SolidWorks API Reference) for full enum values.

NOTE: SolidWorks API calls frequently return None or fail silently.
Every operation checks its result and logs failures rather than crashing.
"""

import logging
import math

from executor.feature_namer import rename_feature
from executor.sw_connection import SolidWorksConnection
from models.operations import Operation, OperationType, ReconstructionPlan

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# SolidWorks API constants (from swconst.h)
# -------------------------------------------------------------------------
swFrontPlane = "Front Plane"
swTopPlane = "Top Plane"
swRightPlane = "Right Plane"

# swEndCondType_e
swEndCondBlind = 0
swEndCondThroughAll = 1
swEndCondMidPlane = 6

# swStartConditions_e
swStartSketchPlane = 0

# swThinWallType_e (not used in basic extrude)

# Plane name lookup
_PLANE_NAMES = {
    "front": swFrontPlane,
    "top": swTopPlane,
    "right": swRightPlane,
}

# mm → metres conversion (SolidWorks API uses metres internally)
_MM = 1e-3


class SolidWorksExecutor:
    """
    Executes a ReconstructionPlan against a live SolidWorks document.

    Usage:
        conn = SolidWorksConnection(template_path)
        part, fmgr, smgr = conn.new_part()
        executor = SolidWorksExecutor(conn)
        executor.execute_plan(plan)
    """

    def __init__(self, connection: SolidWorksConnection):
        self._conn = connection

    @property
    def _part(self):
        return self._conn.part

    @property
    def _fmgr(self):
        return self._conn.feature_mgr

    @property
    def _smgr(self):
        return self._conn.sketch_mgr

    # ------------------------------------------------------------------
    # Plan execution
    # ------------------------------------------------------------------

    def execute_plan(self, plan: ReconstructionPlan) -> list[dict]:
        """
        Execute all operations in plan.operations sequentially.
        Returns a list of result dicts: {"step": int, "success": bool, "detail": str}
        """
        results = []
        logger.info(
            "Executing reconstruction plan: '%s' (%d operations)",
            plan.summary,
            len(plan.operations),
        )

        for op in plan.operations:
            result = self._dispatch(op)
            results.append(result)
            if not result["success"]:
                logger.warning(
                    "Step %d (%s) FAILED: %s",
                    op.step_number,
                    op.operation_type.value,
                    result["detail"],
                )

        successes = sum(1 for r in results if r["success"])
        logger.info(
            "Plan execution complete: %d/%d operations succeeded.",
            successes,
            len(results),
        )
        return results

    def _dispatch(self, op: Operation) -> dict:
        """Route an Operation to its handler method."""
        handlers = {
            OperationType.NEW_SKETCH: self._new_sketch,
            OperationType.SKETCH_LINE: self._sketch_line,
            OperationType.SKETCH_RECTANGLE: self._sketch_rectangle,
            OperationType.SKETCH_CIRCLE: self._sketch_circle,
            OperationType.SKETCH_ARC: self._sketch_arc,
            OperationType.SKETCH_DIMENSION: self._sketch_dimension,
            OperationType.SKETCH_CONSTRAINT: self._sketch_constraint,
            OperationType.CLOSE_SKETCH: self._close_sketch,
            OperationType.EXTRUDE_BOSS: self._extrude_boss,
            OperationType.EXTRUDE_CUT: self._extrude_cut,
            OperationType.REVOLVE_BOSS: self._revolve_boss,
            OperationType.REVOLVE_CUT: self._revolve_cut,
            OperationType.FILLET: self._fillet,
            OperationType.CHAMFER: self._chamfer,
            OperationType.HOLE_WIZARD: self._hole_wizard,
            OperationType.LINEAR_PATTERN: self._linear_pattern,
            OperationType.CIRCULAR_PATTERN: self._circular_pattern,
            OperationType.MIRROR: self._mirror,
            OperationType.SHELL: self._shell,
        }
        handler = handlers.get(op.operation_type)
        if handler is None:
            return {"step": op.step_number, "success": False, "detail": f"No handler for {op.operation_type}"}

        try:
            detail = handler(op)
            return {"step": op.step_number, "success": True, "detail": detail or "OK"}
        except Exception as e:
            logger.exception("Exception in step %d (%s):", op.step_number, op.operation_type.value)
            return {"step": op.step_number, "success": False, "detail": str(e)}

    # ------------------------------------------------------------------
    # Sketch operations
    # ------------------------------------------------------------------

    def _new_sketch(self, op: Operation) -> str:
        p = op.parameters
        plane_spec = p.get("plane", "front")

        if isinstance(plane_spec, str):
            plane_name = _PLANE_NAMES.get(plane_spec.lower(), swFrontPlane)
            self._part.Extension.SelectByID2(plane_name, "PLANE", 0, 0, 0, False, 0, None, 0)
        elif isinstance(plane_spec, dict) and plane_spec.get("type") == "face_ref":
            feature_name = plane_spec.get("feature", "")
            face_index = plane_spec.get("face_index", 0)
            self._part.Extension.SelectByID2(feature_name, "FACE", 0, 0, 0, False, face_index, None, 0)
        else:
            self._part.Extension.SelectByID2(swFrontPlane, "PLANE", 0, 0, 0, False, 0, None, 0)

        self._smgr.InsertSketch(True)
        return f"Opened sketch on plane: {plane_spec}"

    def _sketch_line(self, op: Operation) -> str:
        p = op.parameters
        start = p.get("start", [0, 0])
        end = p.get("end", [0, 0])
        line = self._smgr.CreateLine(
            start[0] * _MM, start[1] * _MM, 0,
            end[0] * _MM, end[1] * _MM, 0,
        )
        if line is None:
            raise RuntimeError(f"CreateLine returned None for {start} → {end}")
        return f"Line {start} → {end}"

    def _sketch_rectangle(self, op: Operation) -> str:
        p = op.parameters
        if "center" in p:
            cx, cy = p["center"]
            w = p.get("width", 10.0)
            h = p.get("height", 10.0)
            rect = self._smgr.CreateCenterRectangle(
                cx * _MM, cy * _MM, 0,
                (cx + w / 2) * _MM, (cy + h / 2) * _MM, 0,
            )
        else:
            c1 = p.get("corner1", [0, 0])
            c2 = p.get("corner2", [10, 10])
            rect = self._smgr.CreateCornerRectangle(
                c1[0] * _MM, c1[1] * _MM, 0,
                c2[0] * _MM, c2[1] * _MM, 0,
            )
        if rect is None:
            raise RuntimeError("CreateRectangle returned None")
        return "Rectangle created"

    def _sketch_circle(self, op: Operation) -> str:
        p = op.parameters
        cx, cy = p.get("center", [0, 0])
        r = p.get("radius", 5.0)
        circle = self._smgr.CreateCircle(cx * _MM, cy * _MM, 0, r * _MM, 0, 0)
        if circle is None:
            raise RuntimeError("CreateCircle returned None")
        return f"Circle center=({cx},{cy}) r={r}"

    def _sketch_arc(self, op: Operation) -> str:
        p = op.parameters
        cx, cy = p.get("center", [0, 0])
        sx, sy = p.get("start", [0, 5])
        ex, ey = p.get("end", [5, 0])
        arc = self._smgr.CreateArc(
            cx * _MM, cy * _MM, 0,
            sx * _MM, sy * _MM, 0,
            ex * _MM, ey * _MM, 0,
            1,  # direction: 1=CW, -1=CCW
        )
        if arc is None:
            raise RuntimeError("CreateArc returned None")
        return f"Arc created center=({cx},{cy})"

    def _sketch_dimension(self, op: Operation) -> str:
        # Dimensions are added via IModelDoc2.AddDimension2 — requires
        # selecting sketch entities first. This is a simplified stub.
        p = op.parameters
        dim_type = p.get("type", "horizontal")
        value = p.get("value", 0.0)
        # In practice: select the entity refs, then call AddDimension2
        # and set the value on the returned IDisplayDimension object.
        logger.debug("Sketch dimension: type=%s value=%s (stub — manual placement needed)", dim_type, value)
        return f"Dimension {dim_type}={value} (stub)"

    def _sketch_constraint(self, op: Operation) -> str:
        # Geometric constraints (coincident, parallel, etc.) are added via
        # ISketchManager.SketchAddConstraints — requires entity selection.
        logger.debug("Sketch constraint: %s (stub)", op.parameters)
        return "Constraint (stub)"

    def _close_sketch(self, op: Operation) -> str:
        self._smgr.InsertSketch(True)  # toggle off = close sketch
        return "Sketch closed"

    # ------------------------------------------------------------------
    # Feature operations
    # ------------------------------------------------------------------

    def _extrude_boss(self, op: Operation) -> str:
        p = op.parameters
        depth = p.get("depth", 10.0) * _MM
        direction = p.get("direction", "normal")
        draft_angle = math.radians(p.get("draft_angle", 0.0))

        end_cond = swEndCondBlind
        if direction == "mid_plane":
            end_cond = swEndCondMidPlane

        feature = self._fmgr.FeatureExtrusion3(
            True,           # SingleDirectionExtrude
            False,          # IsSolid
            False,          # IsThin
            False,          # IsSurface
            True,           # ReverseDir
            False,          # BothDirections
            end_cond,       # StartCondition (swEndCondBlind)
            end_cond,       # EndCondition
            depth,          # Depth (metres)
            0,              # Depth2 (second direction)
            False,          # FlipSideToMaterial
            False,          # FlipSideToMaterial2
            draft_angle,    # DraftAngle
            0,              # DraftAngle2
            True,           # StartOffset (MergeResult)
            False,          # OffsetReverse
            False,          # TranslateSurface
            True,           # NormalCut
            False,          # UseFeatScope
            True,           # UseAutoSelect
            0,              # FeatureScope
            True,           # AssemblyFeatureScope
            False,          # AutoSelectComponents
            False,          # PropagateFeatureToParts
        )

        if feature is None:
            raise RuntimeError("FeatureExtrusion3 returned None — sketch may not be closed or selected.")

        rename_feature(feature, op.description or f"Extrude Boss {op.step_number}")
        return f"Extrude boss depth={p.get('depth')} mm"

    def _extrude_cut(self, op: Operation) -> str:
        p = op.parameters
        depth_val = p.get("depth", "through_all")

        if depth_val == "through_all":
            end_cond = swEndCondThroughAll
            depth = 0.0
        else:
            end_cond = swEndCondBlind
            depth = float(depth_val) * _MM

        feature = self._fmgr.FeatureCut4(
            True,           # SingleDirectionCut
            False,          # BothDirections
            True,           # ReverseDir
            end_cond,       # EndCondition
            end_cond,       # EndCondition2
            depth,          # Depth (metres)
            0,              # Depth2
            False,          # FlipSideToMaterial
            False,          # FlipSideToMaterial2
            False,          # NormalCut
            False,          # UseFeatScope
            True,           # UseAutoSelect
            0,              # FeatureScope
            True,           # AssemblyFeatureScope
            False,          # AutoSelectComponents
            False,          # PropagateFeatureToParts
        )

        if feature is None:
            raise RuntimeError("FeatureCut4 returned None — sketch may not be closed or selected.")

        rename_feature(feature, op.description or f"Extrude Cut {op.step_number}")
        return f"Extrude cut depth={depth_val}"

    def _revolve_boss(self, op: Operation) -> str:
        p = op.parameters
        angle = math.radians(p.get("angle", 360.0))
        axis = p.get("axis", "x")

        # Select axis (for standard axes, use named sketch line or ref axis)
        axis_name = _resolve_axis_name(axis)
        if axis_name:
            self._part.Extension.SelectByID2(axis_name, "EXTSKETCHSEGMENT", 0, 0, 0, True, 16, None, 0)

        feature = self._fmgr.FeatureRevolve2(
            True,       # SingleDirection
            True,       # IsSolid
            False,      # IsThin
            False,      # IsSurface
            False,      # MergeResult
            False,      # ReverseDir
            False,      # ReverseDir2
            0,          # StartCondition (blind)
            0,          # EndCondition
            angle,      # Angle
            0,          # Angle2
            False,      # UseFeatScope
            True,       # UseAutoSelect
        )

        if feature is None:
            raise RuntimeError("FeatureRevolve2 returned None.")

        rename_feature(feature, op.description or f"Revolve Boss {op.step_number}")
        return f"Revolve boss angle={p.get('angle')} deg"

    def _revolve_cut(self, op: Operation) -> str:
        p = op.parameters
        angle = math.radians(p.get("angle", 360.0))
        axis = p.get("axis", "x")

        axis_name = _resolve_axis_name(axis)
        if axis_name:
            self._part.Extension.SelectByID2(axis_name, "EXTSKETCHSEGMENT", 0, 0, 0, True, 16, None, 0)

        feature = self._fmgr.FeatureRevolve2(
            True, False, False, False, False, True, False, 0, 0, angle, 0, False, True
        )

        if feature is None:
            raise RuntimeError("FeatureRevolve2 (cut) returned None.")

        rename_feature(feature, op.description or f"Revolve Cut {op.step_number}")
        return f"Revolve cut angle={p.get('angle')} deg"

    def _fillet(self, op: Operation) -> str:
        p = op.parameters
        radius = p.get("radius", 1.0) * _MM
        edge_selection = p.get("edge_selection", "")

        # NOTE: Edge selection must be done before calling FeatureFillet3.
        # The caller (or a future interactive step) is responsible for selecting
        # edges via SelectByID2 with type "EDGE".
        logger.info("Fillet: r=%s mm, edges: %s (edge selection must be pre-done)", p.get("radius"), edge_selection)

        feature = self._fmgr.FeatureFillet3(
            195,        # FeatureFilletOptions bitmask (default constant-radius)
            radius,     # Radius
            radius,     # Radius2 (for variable, unused here)
            0,          # FilletType (0 = constant radius)
            0,          # KnotPoints
            0,          # SetbackDistance
            False,      # SmoothedFilletEdges
            False,      # PropagateToTangentFaces
        )

        if feature is None:
            raise RuntimeError("FeatureFillet3 returned None — no edges selected.")

        rename_feature(feature, op.description or f"Fillet R{p.get('radius')}mm")
        return f"Fillet r={p.get('radius')} mm"

    def _chamfer(self, op: Operation) -> str:
        p = op.parameters
        distance = p.get("distance", 1.0) * _MM
        angle = math.radians(p.get("angle", 45.0))
        edge_selection = p.get("edge_selection", "")

        logger.info("Chamfer: d=%s mm, angle=%s deg, edges: %s", p.get("distance"), p.get("angle", 45), edge_selection)

        feature = self._fmgr.FeatureChamfer(
            1,          # ChamferType: 1 = Distance-Angle
            distance,
            angle,
            False,      # FlipDirection
        )

        if feature is None:
            raise RuntimeError("FeatureChamfer returned None — no edges selected.")

        rename_feature(feature, op.description or f"Chamfer {p.get('distance')}mm")
        return f"Chamfer d={p.get('distance')} mm"

    def _hole_wizard(self, op: Operation) -> str:
        p = op.parameters
        hole_type = p.get("type", "simple")
        diameter = p.get("diameter", 5.0) * _MM
        depth_val = p.get("depth", "through_all")
        depth = 0.0 if depth_val == "through_all" else float(depth_val) * _MM
        end_cond = swEndCondThroughAll if depth_val == "through_all" else swEndCondBlind

        # Hole Wizard type constants (swWzdHoleTypes_e)
        hole_type_map = {
            "simple": 0,
            "counterbore": 1,
            "countersink": 2,
            "tapped": 3,
        }
        wiz_type = hole_type_map.get(hole_type, 0)

        position = p.get("position", [0, 0])

        feature = self._fmgr.HoleWizard5(
            wiz_type,       # HoleType
            0,              # Standard (0 = none / custom)
            0,              # FastenerType
            end_cond,       # EndCondition
            depth,          # Depth
            diameter,       # Diameter
            0,              # DrillAngle (radians)
            0,              # CounterboreDiameter
            0,              # CounterboreDepth
            0,              # CountersinkAngle
            False,          # ReverseDir
            False,          # FlipSideToMaterial
        )

        if feature is None:
            raise RuntimeError("HoleWizard5 returned None — face/sketch plane may not be selected.")

        rename_feature(feature, op.description or f"Hole Wizard {hole_type} d={p.get('diameter')}mm")
        return f"Hole wizard: {hole_type} d={p.get('diameter')} mm"

    def _linear_pattern(self, op: Operation) -> str:
        p = op.parameters
        feature_ref = p.get("feature_ref", "")
        direction = p.get("direction", [1, 0, 0])
        count = int(p.get("count", 2))
        spacing = p.get("spacing", 10.0) * _MM

        # Select the feature to pattern
        self._part.Extension.SelectByID2(feature_ref, "BODYFEATURE", 0, 0, 0, False, 0, None, 0)

        feature = self._fmgr.FeatureLinearPattern4(
            count,      # D1TotalInstances
            spacing,    # D1Spacing
            1,          # D2TotalInstances
            0,          # D2Spacing
            True,       # PatternSeedOnly
            False,      # GeometryPattern
            False,      # ReverseDir
            False,      # ReverseDir2
            False,      # UseFeatScope
            True,       # UseAutoSelect
        )

        if feature is None:
            raise RuntimeError(f"FeatureLinearPattern4 returned None for feature_ref='{feature_ref}'.")

        rename_feature(feature, op.description or f"Linear Pattern x{count}")
        return f"Linear pattern of '{feature_ref}' count={count} spacing={p.get('spacing')} mm"

    def _circular_pattern(self, op: Operation) -> str:
        p = op.parameters
        feature_ref = p.get("feature_ref", "")
        count = int(p.get("count", 4))
        angle = math.radians(p.get("angle", 360.0))

        # Select the feature to pattern
        self._part.Extension.SelectByID2(feature_ref, "BODYFEATURE", 0, 0, 0, False, 0, None, 0)

        feature = self._fmgr.FeatureCircularPattern4(
            count,      # D1TotalInstances
            angle,      # D1Spacing (total angle in radians)
            True,       # EquallySpaced
            False,      # PatternSeedOnly
            False,      # GeometryPattern
            False,      # ReverseDir
            False,      # UseFeatScope
            True,       # UseAutoSelect
        )

        if feature is None:
            raise RuntimeError(f"FeatureCircularPattern4 returned None for feature_ref='{feature_ref}'.")

        rename_feature(feature, op.description or f"Circular Pattern x{count}")
        return f"Circular pattern of '{feature_ref}' count={count}"

    def _mirror(self, op: Operation) -> str:
        p = op.parameters
        feature_refs = p.get("feature_refs", [])
        plane = p.get("plane", "right")

        plane_name = _PLANE_NAMES.get(plane.lower(), swRightPlane)
        self._part.Extension.SelectByID2(plane_name, "PLANE", 0, 0, 0, False, 0, None, 0)

        # Select features to mirror (add to selection)
        for ref in feature_refs:
            self._part.Extension.SelectByID2(ref, "BODYFEATURE", 0, 0, 0, True, 0, None, 0)

        feature = self._fmgr.FeatureMirror(False, False)

        if feature is None:
            raise RuntimeError(f"FeatureMirror returned None for features={feature_refs}.")

        rename_feature(feature, op.description or f"Mirror {', '.join(feature_refs)}")
        return f"Mirror of {feature_refs} across {plane}"

    def _shell(self, op: Operation) -> str:
        p = op.parameters
        thickness = p.get("thickness", 2.0) * _MM
        faces_desc = p.get("faces_to_remove", "")

        logger.info("Shell: thickness=%s mm, open faces: %s (face selection must be pre-done)", p.get("thickness"), faces_desc)

        feature = self._fmgr.InsertFeatureShell(thickness, False)

        if feature is None:
            raise RuntimeError("InsertFeatureShell returned None — no faces selected.")

        rename_feature(feature, op.description or f"Shell t={p.get('thickness')}mm")
        return f"Shell thickness={p.get('thickness')} mm"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _resolve_axis_name(axis) -> str:
    """Map an axis specifier to a SolidWorks entity name for SelectByID2."""
    if isinstance(axis, str):
        return {"x": "Line1", "y": "Line2"}.get(axis.lower(), "")
    return ""
