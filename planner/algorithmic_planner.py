"""
Algorithmic (rule-based) reconstruction planner.

No AI/API required — derives the SolidWorks operation sequence directly
from the extracted geometry and detected features produced by StepExtractor
and FeatureRecognizer.

Strategy:
  1. Classify part as prismatic (extrude) or turned (revolve).
  2. Pick the best base plane using area-weighted face-normal voting.
  3. Extract the base sketch profile from the face's boundary edges
     (falls back to bounding-box rectangle when edges are unavailable).
  4. Emit hole_wizard operations for each detected hole.
  5. Emit linear/circular patterns to replace repeated holes.
  6. Emit chamfer, then fillet operations (always last).
"""

import logging
import math
from typing import Optional

from models.geometry import EdgeType, Face, FaceType, PartGeometry, Vector3D
from models.operations import Operation, OperationType, ReconstructionPlan, SketchPlane

logger = logging.getLogger(__name__)

# Tolerance for floating-point comparisons (mm / unitless)
_TOL = 0.01
# Angle tolerance for "parallel normal" check (radians)
_ANGLE_TOL = math.radians(5.0)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class AlgorithmicPlanner:
    """Derives a SolidWorks ReconstructionPlan directly from PartGeometry."""

    def plan(self, geometry: PartGeometry) -> ReconstructionPlan:
        features = geometry.detected_features or []

        # 1. Classify
        is_turned = _is_turned_part(geometry)

        # 2. Base plane + extrude direction
        base_plane_str, extrude_dir = _determine_base_plane(geometry)

        ops: list[Operation] = []
        step = 1

        # 3. Base body (extrude or revolve)
        if is_turned:
            body_ops, strategy = _build_revolve_ops(geometry, step)
        else:
            body_ops, strategy = _build_extrude_ops(geometry, base_plane_str, extrude_dir, step)

        for op in body_ops:
            op.step_number = step
            step += 1
        ops.extend(body_ops)

        # 4. Holes — figure out which are seeds for patterns
        patterned_ids = _get_patterned_hole_ids(features)
        seed_ids = _get_pattern_seed_ids(features)
        hole_name_map: dict[str, str] = {}  # feature_id -> SW feature name

        for feat in features:
            if feat["type"] not in ("through_hole", "blind_hole"):
                continue
            fid = feat["id"]
            if fid in patterned_ids and fid not in seed_ids:
                continue  # non-seed patterned hole — emitted via pattern op
            hole_ops = _build_hole_ops(feat, geometry)
            # The extrude_cut is the last op; its step number determines the feature name
            cut_step = step + len(hole_ops) - 1
            feat_name = f"Hole_{cut_step}"
            hole_ops[-1].description = feat_name  # SW will rename cut to this
            hole_name_map[fid] = feat_name
            for op in hole_ops:
                op.step_number = step
                step += 1
            ops.extend(hole_ops)

        # 5. Patterns
        for feat in features:
            if feat["type"] == "linear_pattern":
                seed_id = feat["feature_ids"][0]
                seed_name = hole_name_map.get(seed_id, "Hole_1")
                pat_op = _build_linear_pattern_op(feat, seed_name, step)
                ops.append(pat_op)
                step += 1
            elif feat["type"] == "circular_pattern":
                seed_id = feat["feature_ids"][0]
                seed_name = hole_name_map.get(seed_id, "Hole_1")
                pat_op = _build_circular_pattern_op(feat, seed_name, step)
                ops.append(pat_op)
                step += 1

        # 6. Chamfers (before fillets)
        for feat in features:
            if feat["type"] == "chamfer":
                ops.append(_build_chamfer_op(feat, step))
                step += 1

        # 7. Fillets (always last)
        for feat in features:
            if feat["type"] == "fillet":
                ops.append(_build_fillet_op(feat, step))
                step += 1

        # Build summary
        dims = _bounding_box_dims(geometry)
        part_type_label = "Turned" if is_turned else "Prismatic"
        summary = (
            f"{part_type_label} part — "
            f"{dims[0]:.1f} × {dims[1]:.1f} × {dims[2]:.1f} mm"
        )

        notes = _build_notes(geometry, is_turned, base_plane_str, ops)

        return ReconstructionPlan(
            summary=summary,
            base_plane=SketchPlane(base_plane_str),
            modeling_strategy=strategy,
            operations=ops,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Part classification
# ---------------------------------------------------------------------------

def _is_turned_part(geometry: PartGeometry) -> bool:
    """
    Return True if the part is primarily a turned/rotational part.
    Criteria: rotational symmetry detected, OR cylindrical faces make up
    >40 % of total surface area.
    """
    sym = geometry.symmetry_planes or []
    # "XZ" and "YZ" both present → rotational symmetry around Z
    if len(sym) >= 2:
        return True

    total_area = sum(f.area for f in geometry.faces)
    if total_area < 1e-10:
        return False
    cyl_area = sum(f.area for f in geometry.faces if f.face_type == FaceType.CYLINDRICAL)
    return (cyl_area / total_area) > 0.40


# ---------------------------------------------------------------------------
# Base plane selection
# ---------------------------------------------------------------------------

def _determine_base_plane(geometry: PartGeometry) -> tuple[str, Vector3D]:
    """
    Pick the sketch plane whose normal best represents the dominant face orientation.
    Uses area-weighted voting across all planar faces.
    Returns (plane_name, extrude_direction_vector).
    """
    _axis_map = {
        "top":   Vector3D(0, 0, 1),
        "front": Vector3D(0, 1, 0),
        "right": Vector3D(1, 0, 0),
    }
    weights = {"top": 0.0, "front": 0.0, "right": 0.0}

    for f in geometry.faces:
        if f.face_type != FaceType.PLANAR or not f.normal:
            continue
        n = f.normal
        mag = math.sqrt(n.x**2 + n.y**2 + n.z**2)
        if mag < 1e-10:
            continue
        weights["top"]   += f.area * abs(n.z) / mag
        weights["front"] += f.area * abs(n.y) / mag
        weights["right"] += f.area * abs(n.x) / mag

    best = max(weights, key=lambda k: weights[k])
    return best, _axis_map[best]


# ---------------------------------------------------------------------------
# Base body — extrude
# ---------------------------------------------------------------------------

def _build_extrude_ops(
    geometry: PartGeometry,
    base_plane: str,
    extrude_dir: Vector3D,
    _start_step: int,
) -> tuple[list[Operation], str]:
    bb_min = geometry.bounding_box_min
    bb_max = geometry.bounding_box_max

    # Width, height in sketch plane; depth along extrude direction
    if base_plane == "top":
        width  = bb_max.x - bb_min.x
        height = bb_max.y - bb_min.y
        depth  = bb_max.z - bb_min.z
        cx = (bb_min.x + bb_max.x) / 2
        cy = (bb_min.y + bb_max.y) / 2
    elif base_plane == "front":
        width  = bb_max.x - bb_min.x
        height = bb_max.z - bb_min.z
        depth  = bb_max.y - bb_min.y
        cx = (bb_min.x + bb_max.x) / 2
        cy = (bb_min.z + bb_max.z) / 2
    else:  # right
        width  = bb_max.y - bb_min.y
        height = bb_max.z - bb_min.z
        depth  = bb_max.x - bb_min.x
        cx = (bb_min.y + bb_max.y) / 2
        cy = (bb_min.z + bb_max.z) / 2

    # Try to extract accurate profile from face edges
    profile_ops = _extract_sketch_profile(geometry, base_plane)

    ops: list[Operation] = [
        Operation(
            step_number=0,
            operation_type=OperationType.NEW_SKETCH,
            parameters={"plane": base_plane},
            description=f"Base sketch on {base_plane} plane",
            references=[],
        ),
    ]

    if profile_ops:
        ops.extend(profile_ops)
    else:
        # Fallback: bounding-box rectangle
        half_w = width / 2
        half_h = height / 2
        ops.append(Operation(
            step_number=0,
            operation_type=OperationType.SKETCH_RECTANGLE,
            parameters={
                "corner1": [round(cx - half_w, 3), round(cy - half_h, 3)],
                "corner2": [round(cx + half_w, 3), round(cy + half_h, 3)],
            },
            description=f"Base profile {width:.2f} × {height:.2f} mm (from bounding box)",
            references=[],
        ))

    ops.append(Operation(
        step_number=0,
        operation_type=OperationType.CLOSE_SKETCH,
        parameters={},
        description="Close base sketch",
        references=[],
    ))
    ops.append(Operation(
        step_number=0,
        operation_type=OperationType.EXTRUDE_BOSS,
        parameters={"depth": round(depth, 3), "direction": "normal"},
        description=f"Extrude base body {depth:.2f} mm",
        references=["Base_Sketch"],
    ))

    strategy = (
        f"Prismatic part extruded {depth:.2f} mm from {base_plane} plane. "
        f"Profile {width:.2f} × {height:.2f} mm."
    )
    return ops, strategy


# ---------------------------------------------------------------------------
# Base body — revolve
# ---------------------------------------------------------------------------

def _build_revolve_ops(
    geometry: PartGeometry,
    _start_step: int,
) -> tuple[list[Operation], str]:
    bb_min = geometry.bounding_box_min
    bb_max = geometry.bounding_box_max

    # Choose revolution axis = longest bounding-box dimension
    dims_xyz = [
        bb_max.x - bb_min.x,
        bb_max.y - bb_min.y,
        bb_max.z - bb_min.z,
    ]
    max_idx = dims_xyz.index(max(dims_xyz))
    axis_names = ["x", "y", "z"]
    rev_axis = axis_names[max_idx]

    # Sketch plane perpendicular to the revolution axis but containing it
    # Conventionally: revolve around Y → sketch on front; around Z → front; around X → right
    plane_for_axis = {"x": "right", "y": "front", "z": "front"}
    sketch_plane = plane_for_axis[rev_axis]

    # Build profile from cylindrical face radii and axial extents
    profile_ops = _build_revolved_profile(geometry, rev_axis)

    ops: list[Operation] = [
        Operation(
            step_number=0,
            operation_type=OperationType.NEW_SKETCH,
            parameters={"plane": sketch_plane},
            description=f"Revolution profile sketch on {sketch_plane} plane",
            references=[],
        ),
        *profile_ops,
        Operation(
            step_number=0,
            operation_type=OperationType.CLOSE_SKETCH,
            parameters={},
            description="Close revolution sketch",
            references=[],
        ),
        Operation(
            step_number=0,
            operation_type=OperationType.REVOLVE_BOSS,
            parameters={"axis": rev_axis, "angle": 360.0},
            description=f"Revolve profile 360° around {rev_axis.upper()} axis",
            references=[],
        ),
    ]

    strategy = (
        f"Turned/rotational part. Revolved 360° around {rev_axis.upper()} axis. "
        f"Profile built from cylindrical face data."
    )
    return ops, strategy


def _build_revolved_profile(
    geometry: PartGeometry, rev_axis: str
) -> list[Operation]:
    """
    Build sketch lines for the stepped cross-section of a turned part.
    Projects cylindrical face data onto the revolution plane.
    """
    # Helper: get the axial coordinate and radius for each cylindrical face
    def axis_coord(center: Vector3D) -> float:
        return {"x": center.x, "y": center.y, "z": center.z}[rev_axis]

    cyl_faces = [
        f for f in geometry.faces
        if f.face_type == FaceType.CYLINDRICAL and f.center and f.radius and f.radius > _TOL
    ]
    if not cyl_faces:
        # No cylindrical data — fall back to bounding box profile
        bb = geometry.bounding_box_min
        bx = geometry.bounding_box_max
        length = {"x": bx.x - bb.x, "y": bx.y - bb.y, "z": bx.z - bb.z}[rev_axis]
        max_r = max((bx.x - bb.x, bx.y - bb.y, bx.z - bb.z)) / 2
        # Simple rectangle profile
        return [Operation(
            step_number=0,
            operation_type=OperationType.SKETCH_RECTANGLE,
            parameters={
                "corner1": [0.0, 0.0],
                "corner2": [round(length, 3), round(max_r, 3)],
            },
            description="Revolution profile (bounding box fallback)",
            references=[],
        )]

    # Group by rounded radius, find axial extent per step
    # Sort unique radii descending (largest first = outer profile)
    unique_radii = sorted(set(round(f.radius, 2) for f in cyl_faces), reverse=True)

    # For each radius band, find axial min/max
    segments: list[tuple[float, float, float]] = []  # (radius, ax_min, ax_max)
    for r in unique_radii:
        group = [f for f in cyl_faces if abs(f.radius - r) < 0.05]
        coords = [axis_coord(f.center) for f in group]
        segments.append((r, min(coords), max(coords)))

    # Sort by axial position
    segments.sort(key=lambda s: s[1])

    # Emit sketch lines forming the stepped profile
    # Profile lives in the positive-radius half (above the axis)
    ops: list[Operation] = []
    prev_r = 0.0
    prev_ax = segments[0][1]

    # Opening: horizontal line at axis from start to first segment
    # Then step up to first radius, travel axially, step through radii

    for i, (r, ax_min, ax_max) in enumerate(segments):
        # Vertical step up from previous radius to this radius (if needed)
        if abs(r - prev_r) > _TOL:
            ops.append(Operation(
                step_number=0,
                operation_type=OperationType.SKETCH_LINE,
                parameters={
                    "start": [round(prev_ax, 3), round(prev_r, 3)],
                    "end":   [round(ax_min, 3),  round(r, 3)],
                },
                description=f"Step to radius {r:.2f} mm",
                references=[],
            ))
        # Horizontal line along this radius
        ops.append(Operation(
            step_number=0,
            operation_type=OperationType.SKETCH_LINE,
            parameters={
                "start": [round(ax_min, 3), round(r, 3)],
                "end":   [round(ax_max, 3), round(r, 3)],
            },
            description=f"Profile at radius {r:.2f} mm",
            references=[],
        ))
        prev_r = r
        prev_ax = ax_max

    # Close profile back to axis
    ops.append(Operation(
        step_number=0,
        operation_type=OperationType.SKETCH_LINE,
        parameters={
            "start": [round(prev_ax, 3), round(prev_r, 3)],
            "end":   [round(prev_ax, 3), 0.0],
        },
        description="Close profile to axis",
        references=[],
    ))
    ops.append(Operation(
        step_number=0,
        operation_type=OperationType.SKETCH_LINE,
        parameters={
            "start": [round(prev_ax, 3), 0.0],
            "end":   [round(segments[0][1], 3), 0.0],
        },
        description="Axis line (close revolution profile)",
        references=[],
    ))

    return ops


# ---------------------------------------------------------------------------
# Sketch profile extraction from face edges
# ---------------------------------------------------------------------------

def _extract_sketch_profile(
    geometry: PartGeometry, base_plane: str
) -> list[Operation]:
    """
    Find the largest planar face perpendicular to the extrude direction,
    locate its boundary edges, and convert them to sketch operations.
    Returns an empty list if extraction fails (caller will use fallback).
    """
    normal_map = {
        "top":   Vector3D(0, 0, 1),
        "front": Vector3D(0, 1, 0),
        "right": Vector3D(1, 0, 0),
    }
    target_normal = normal_map[base_plane]

    # Find candidate faces: planar, normal roughly parallel to target
    candidates = [
        f for f in geometry.faces
        if f.face_type == FaceType.PLANAR
        and f.normal
        and _normals_parallel(f.normal, target_normal, _ANGLE_TOL)
    ]
    if not candidates:
        return []

    base_face = max(candidates, key=lambda f: f.area)

    # Edges that border the base face
    face_edges = [
        e for e in geometry.edges
        if base_face.id in e.adjacent_face_ids
    ]
    if not face_edges:
        return []

    ops: list[Operation] = []
    for edge in face_edges:
        op = _edge_to_sketch_op(edge, base_plane)
        if op is not None:
            ops.append(op)

    return ops


def _edge_to_sketch_op(edge, base_plane: str) -> Optional[Operation]:
    """Project a 3D edge to a 2D sketch operation."""

    def proj(v: Vector3D) -> list[float]:
        if base_plane == "top":
            return [round(v.x, 3), round(v.y, 3)]
        elif base_plane == "front":
            return [round(v.x, 3), round(v.z, 3)]
        else:  # right
            return [round(v.y, 3), round(v.z, 3)]

    if edge.edge_type == EdgeType.LINE:
        return Operation(
            step_number=0,
            operation_type=OperationType.SKETCH_LINE,
            parameters={
                "start": proj(edge.start_point),
                "end":   proj(edge.end_point),
            },
            description="Profile edge",
            references=[],
        )

    if edge.edge_type == EdgeType.CIRCLE and edge.center and edge.radius:
        return Operation(
            step_number=0,
            operation_type=OperationType.SKETCH_CIRCLE,
            parameters={
                "center": proj(edge.center),
                "radius": round(edge.radius, 3),
            },
            description=f"Profile circle r={edge.radius:.2f} mm",
            references=[],
        )

    if edge.edge_type == EdgeType.ARC and edge.center and edge.radius:
        return Operation(
            step_number=0,
            operation_type=OperationType.SKETCH_ARC,
            parameters={
                "center": proj(edge.center),
                "start":  proj(edge.start_point),
                "end":    proj(edge.end_point),
            },
            description=f"Profile arc r={edge.radius:.2f} mm",
            references=[],
        )

    return None  # ellipse / bspline — skip


# ---------------------------------------------------------------------------
# Hole operations
# ---------------------------------------------------------------------------

def _build_hole_ops(feat: dict, geometry: PartGeometry) -> list[Operation]:
    """
    Convert a detected hole into sketch + extrude_cut operations.
    Returns 4 ops: new_sketch, sketch_circle, close_sketch, extrude_cut.
    Step numbers are left at 0 — caller assigns them.
    """
    cx, cy, cz = feat["center"]
    ax, ay, az = feat["axis"]
    diameter = feat["diameter"]
    raw_depth = feat.get("depth", "through_all")
    depth = raw_depth if raw_depth == "through_all" else round(float(raw_depth), 3)

    # Use the standard reference plane perpendicular to the hole axis.
    # Avoids SelectByID2 face selection, which fails on freshly extruded solids.
    # For through_all cuts this works regardless of which face the hole enters from.
    #
    # LIMITATION: if the hole axis is not well-aligned with any of the three
    # origin planes (e.g. a hole drilled into a 45° angled face), this produces
    # a cylinder perpendicular to the chosen standard plane rather than the true
    # angled cylinder.  Such holes require a reference plane created via
    # InsertRefPlane — not yet implemented.  The planner adds a note when it
    # detects significant axis misalignment.
    dominant = max(abs(ax), abs(ay), abs(az))
    alignment_ok = dominant > 0.9  # axis is within ~26° of a principal direction

    if abs(az) >= abs(ax) and abs(az) >= abs(ay):
        # Z-axis hole — sketch on Top plane; sketch coords are (X, Y)
        plane = "top"
        circle_center = [round(cx, 3), round(cy, 3)]
    elif abs(ay) >= abs(ax):
        # Y-axis hole — sketch on Front plane; sketch coords are (X, Z)
        plane = "front"
        circle_center = [round(cx, 3), round(cz, 3)]
    else:
        # X-axis hole — sketch on Right plane; sketch coords are (Y, Z)
        plane = "right"
        circle_center = [round(cy, 3), round(cz, 3)]

    depth_label = "through" if depth == "through_all" else f"{depth:.1f} mm"
    hole_label = f"Hole Ø{diameter:.2f} mm {depth_label}"

    return [
        Operation(
            step_number=0,
            operation_type=OperationType.NEW_SKETCH,
            parameters={"plane": plane},
            description=f"Hole sketch on {plane} plane",
            references=[],
        ),
        Operation(
            step_number=0,
            operation_type=OperationType.SKETCH_CIRCLE,
            parameters={
                "center": circle_center,
                "radius": round(diameter / 2, 3),
            },
            description=f"Hole circle Ø{diameter:.2f} mm",
            references=[],
        ),
        Operation(
            step_number=0,
            operation_type=OperationType.CLOSE_SKETCH,
            parameters={},
            description="Close hole sketch",
            references=[],
        ),
        Operation(
            step_number=0,
            operation_type=OperationType.EXTRUDE_CUT,
            parameters={
                "depth": depth,
                "_axis_aligned": alignment_ok,  # False → angled hole, result may be inaccurate
            },
            description=hole_label,  # caller may overwrite to set feature name
            references=[],
        ),
    ]


# ---------------------------------------------------------------------------
# Pattern operations
# ---------------------------------------------------------------------------

def _build_linear_pattern_op(feat: dict, seed_name: str, step: int) -> Operation:
    direction = [round(v, 4) for v in feat["direction"]]
    return Operation(
        step_number=step,
        operation_type=OperationType.LINEAR_PATTERN,
        parameters={
            "feature_ref": seed_name,
            "direction": direction,
            "count": feat["count"],
            "spacing": round(feat["spacing"], 3),
        },
        description=(
            f"Linear pattern: {feat['count']}× "
            f"at {feat['spacing']:.2f} mm spacing"
        ),
        references=[seed_name],
    )


def _build_circular_pattern_op(feat: dict, seed_name: str, step: int) -> Operation:
    return Operation(
        step_number=step,
        operation_type=OperationType.CIRCULAR_PATTERN,
        parameters={
            "feature_ref": seed_name,
            "axis": feat["axis"],
            "count": feat["count"],
            "angle": 360.0,
        },
        description=(
            f"Circular pattern: {feat['count']}× "
            f"at {feat['angular_spacing']:.1f}° spacing"
        ),
        references=[seed_name],
    )


# ---------------------------------------------------------------------------
# Fillet / chamfer operations
# ---------------------------------------------------------------------------

def _build_fillet_op(feat: dict, step: int) -> Operation:
    return Operation(
        step_number=step,
        operation_type=OperationType.FILLET,
        parameters={
            "radius": round(feat["radius"], 3),
            "edge_selection": (
                f"Edges with fillet radius {feat['radius']:.3f} mm "
                f"({feat['face_count']} faces)"
            ),
        },
        description=f"Fillet R{feat['radius']:.2f} mm ({feat['face_count']} faces)",
        references=[],
    )


def _build_chamfer_op(feat: dict, step: int) -> Operation:
    # Chamfer size is not directly measured by the recognizer — use a note
    return Operation(
        step_number=step,
        operation_type=OperationType.CHAMFER,
        parameters={
            "distance": 1.0,  # placeholder — verify from part
            "angle": 45.0,
            "edge_selection": f"{feat['face_count']} chamfer edges detected",
        },
        description=f"Chamfer ({feat['face_count']} edges) — verify distance",
        references=[],
    )


# ---------------------------------------------------------------------------
# Pattern helper utilities
# ---------------------------------------------------------------------------

def _get_patterned_hole_ids(features: list[dict]) -> set[str]:
    """Return all hole feature IDs that appear in any pattern."""
    ids: set[str] = set()
    for feat in features:
        if feat["type"] in ("linear_pattern", "circular_pattern"):
            ids.update(feat.get("feature_ids", []))
    return ids


def _get_pattern_seed_ids(features: list[dict]) -> set[str]:
    """Return only the first hole ID from each pattern (the seed)."""
    ids: set[str] = set()
    for feat in features:
        if feat["type"] in ("linear_pattern", "circular_pattern"):
            fids = feat.get("feature_ids", [])
            if fids:
                ids.add(fids[0])
    return ids


# ---------------------------------------------------------------------------
# Notes builder
# ---------------------------------------------------------------------------

def _build_notes(
    geometry: PartGeometry, is_turned: bool, base_plane: str,
    ops: list[Operation] | None = None,
) -> list[str]:
    notes: list[str] = [
        "Plan generated algorithmically from STEP geometry — no AI used.",
        "Verify sketch profile against original STEP before executing.",
    ]
    if is_turned:
        notes.append(
            "Part classified as turned/rotational. "
            "Review revolution profile for accuracy."
        )
    if not geometry.detected_features:
        notes.append(
            "No high-level features detected. "
            "Run FeatureRecognizer before planning for better results."
        )

    # Warn if chamfer size is placeholder
    chamfers = [f for f in (geometry.detected_features or []) if f["type"] == "chamfer"]
    if chamfers:
        notes.append(
            "Chamfer distance set to 1.0 mm placeholder — "
            "measure actual chamfer from original STEP."
        )

    # Warn about holes whose axes are not aligned with any origin plane
    if ops:
        misaligned = [
            op for op in ops
            if op.operation_type == OperationType.EXTRUDE_CUT
            and not op.parameters.get("_axis_aligned", True)
        ]
        if misaligned:
            notes.append(
                f"{len(misaligned)} hole(s) have axes not aligned with any origin plane. "
                "The extrude cut was made on the closest standard plane and may be "
                "inaccurate. Angled holes require a reference plane (InsertRefPlane) "
                "— not yet automated."
            )

    return notes


# ---------------------------------------------------------------------------
# Math utilities
# ---------------------------------------------------------------------------

def _bounding_box_dims(geometry: PartGeometry) -> tuple[float, float, float]:
    bb_min = geometry.bounding_box_min
    bb_max = geometry.bounding_box_max
    return (
        bb_max.x - bb_min.x,
        bb_max.y - bb_min.y,
        bb_max.z - bb_min.z,
    )


def _normals_parallel(n1: Vector3D, n2: Vector3D, tol: float) -> bool:
    angle = _angle_between(n1, n2)
    return angle < tol


def _angle_between(a: Vector3D, b: Vector3D) -> float:
    dot = a.x * b.x + a.y * b.y + a.z * b.z
    ma = math.sqrt(a.x**2 + a.y**2 + a.z**2)
    mb = math.sqrt(b.x**2 + b.y**2 + b.z**2)
    if ma < 1e-10 or mb < 1e-10:
        return 0.0
    cos_a = max(-1.0, min(1.0, dot / (ma * mb)))
    angle = math.acos(cos_a)
    return min(angle, math.pi - angle)
