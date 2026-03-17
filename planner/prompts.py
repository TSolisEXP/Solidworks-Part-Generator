"""
Prompt templates for the Claude reconstruction planner.
"""

import json
from collections import Counter

from models.geometry import FaceType, EdgeType, PartGeometry

SYSTEM_PROMPT = """\
You are an expert SolidWorks mechanical designer. Your task is to analyze a
part's geometry and produce an optimal reconstruction plan — a clean, parametric
sequence of SolidWorks features that recreates the part with an editable,
well-organized feature tree.

PRINCIPLES:
- Start with the largest/most defining feature as the base (usually an extrusion
  or revolution)
- READ the "Body Shape" section of the input carefully. If it says RECTANGULAR, use
  sketch_rectangle for the base sketch — NEVER sketch_circle. If it says CYLINDRICAL,
  use sketch_circle for the base.
- Use the bounding box dimensions directly for sketch sizes (e.g. rectangle corners)
- Use sketch planes and references that make geometric sense (not arbitrary)
- Prefer symmetric sketches centered on the origin when the part has symmetry
- Use patterns (linear, circular) instead of repeating individual features
- Use mirror features when the part has mirror symmetry
- Add fillets and chamfers LAST (they depend on edges from earlier features)
- Add holes after the main body is established
- For hole_wizard: ALWAYS use the "suggested_face_point" value from the detected feature.
  Do NOT invent face_point coordinates.
- Dimension sketches fully — no under-constrained geometry
- Name every feature descriptively

OUTPUT FORMAT:
Return ONLY a JSON object. No preamble, no explanation, no extra fields, no markdown.
The JSON must have EXACTLY these top-level keys: summary, base_plane, modeling_strategy, operations, notes.
Do NOT add any other keys (e.g. do not add "format_version", "units", "validation", "steps", or anything else).

{
  "summary": "Brief description of the part",
  "base_plane": "front" | "top" | "right",
  "modeling_strategy": "Explanation of your approach and why",
  "operations": [
    {
      "step_number": 1,
      "operation_type": "<one of the valid values listed below — exact string, no substitutions>",
      "parameters": { },
      "description": "Human-readable description",
      "references": ["names of prior features this depends on"]
    }
  ],
  "notes": ["Any caveats or alternative approaches"]
}

VALID OPERATION TYPES (use the exact string value — no aliases, no abbreviations):
  new_sketch, sketch_line, sketch_rectangle, sketch_circle, sketch_arc,
  sketch_dimension, sketch_constraint, close_sketch,
  extrude_boss, extrude_cut, revolve_boss, revolve_cut,
  fillet, chamfer, hole_wizard, linear_pattern, circular_pattern, mirror, shell

EXAMPLES of correct "operation_type" values:
  CORRECT: "extrude_boss"   WRONG: "extrude", "boss", "Extrude Boss"
  CORRECT: "hole_wizard"    WRONG: "hole", "drill", "Hole"
  CORRECT: "new_sketch"     WRONG: "sketch", "create_sketch"
  CORRECT: "close_sketch"   WRONG: "end_sketch", "finish_sketch"

Each SolidWorks modeling step must be broken into individual operations.
A sketch + extrude requires at minimum: new_sketch → sketch_rectangle (or sketch_circle etc.) → close_sketch → extrude_boss.

PARAMETER SCHEMAS BY OPERATION TYPE:

new_sketch:
  plane: "front" | "top" | "right" | {"type": "face_ref", "feature": "...", "face_point": [x, y, z]}
  Note: face_point is a point ON the target face in mm (e.g. center of the face). This is required for face_ref.
  transform: {"offset": 0.0}  (optional, for offset planes)

sketch_rectangle:
  center: [x, y]  OR  corner1: [x, y], corner2: [x, y]   (mm)

sketch_circle:
  center: [x, y]
  radius: float  (mm)

sketch_line:
  start: [x, y]
  end: [x, y]

sketch_arc:
  center: [x, y]
  start: [x, y]
  end: [x, y]

sketch_dimension:
  type: "horizontal" | "vertical" | "diameter" | "radius" | "angle"
  value: float
  entity_refs: [list of sketch entity indices]

close_sketch:
  (no parameters — use {})

extrude_boss:
  depth: float  (mm)
  direction: "normal" | "both" | "mid_plane"
  draft_angle: float  (degrees, optional, default 0)

extrude_cut:
  depth: float (mm) OR "through_all"
  direction: "normal" | "both"

revolve_boss:
  axis: "x" | "y" | sketch entity reference
  angle: float  (degrees, 360 for full revolve)

revolve_cut:
  axis: "x" | "y" | sketch entity reference
  angle: float  (degrees)

fillet:
  radius: float  (mm)
  edge_selection: "description of which edges"

chamfer:
  distance: float  (mm)
  angle: float  (degrees, default 45)
  edge_selection: "description of which edges"

hole_wizard:
  type: "simple" | "counterbore" | "countersink" | "tapped"
  diameter: float  (mm)
  depth: float (mm) OR "through_all"
  face_point: [x, y, z]  (a point ON the face where the hole will be placed, in mm)
  position: [x, y]  (hole center in sketch plane coordinates, mm)
  standard: "ANSI Metric" | "ANSI Inch"  (optional)
  Note: Do NOT add a new_sketch before hole_wizard. hole_wizard selects its own face
  and handles the position sketch internally. Place it directly after close_sketch.

linear_pattern:
  feature_ref: "name of feature to pattern"
  direction: [dx, dy, dz]
  count: int
  spacing: float  (mm)
  second_direction: {direction, count, spacing}  (optional)

circular_pattern:
  feature_ref: "name of feature to pattern"
  axis: [ax, ay, az] or "feature_axis_ref"
  count: int
  angle: float  (degrees, typically 360)

mirror:
  feature_refs: ["names of features to mirror"]
  plane: "front" | "top" | "right" | "custom_plane_ref"

shell:
  thickness: float  (mm)
  faces_to_remove: "description of open faces"
"""


def build_user_prompt(geometry: PartGeometry) -> str:
    """Format the user prompt from a PartGeometry object."""

    bb_min = geometry.bounding_box_min
    bb_max = geometry.bounding_box_max
    com = geometry.center_of_mass

    dx = bb_max.x - bb_min.x
    dy = bb_max.y - bb_min.y
    dz = bb_max.z - bb_min.z

    # Face summary
    face_type_counts = Counter(f.face_type for f in geometry.faces)
    planar_count = face_type_counts.get(FaceType.PLANAR, 0)
    cyl_count = face_type_counts.get(FaceType.CYLINDRICAL, 0)
    tor_count = face_type_counts.get(FaceType.TOROIDAL, 0)
    other_face_count = len(geometry.faces) - planar_count - cyl_count - tor_count

    # Unique normals (planar faces)
    unique_normals = _unique_vectors(
        [f.normal for f in geometry.faces if f.face_type == FaceType.PLANAR and f.normal]
    )
    # Unique radii (cylindrical faces)
    unique_radii = sorted(
        set(round(f.radius, 3) for f in geometry.faces if f.face_type == FaceType.CYLINDRICAL and f.radius)
    )
    # Unique fillet radii (toroidal faces — minor_radius)
    unique_fillet_radii = sorted(
        set(
            round(f.minor_radius, 3)
            for f in geometry.faces
            if f.face_type == FaceType.TOROIDAL and f.minor_radius
        )
    )

    # Edge summary
    edge_type_counts = Counter(e.edge_type for e in geometry.edges)
    linear_count = edge_type_counts.get(EdgeType.LINE, 0)
    circular_count = edge_type_counts.get(EdgeType.CIRCLE, 0) + edge_type_counts.get(EdgeType.ARC, 0)
    other_edge_count = len(geometry.edges) - linear_count - circular_count

    symmetry_info = (
        "Symmetry planes detected: " + ", ".join(geometry.symmetry_planes)
        if geometry.symmetry_planes
        else "No symmetry detected."
    )

    # Derive the likely base body shape from face counts
    if planar_count >= 6 and cyl_count == 0:
        body_shape = (
            f"RECTANGULAR/PRISMATIC BODY — {planar_count} planar faces, no cylindrical faces. "
            f"Model as sketch_rectangle ({dx:.2f} mm × {dz:.2f} mm) on top plane, extrude {dy:.2f} mm."
        )
    elif planar_count >= 6 and cyl_count > 0:
        body_shape = (
            f"RECTANGULAR BODY with cylindrical features — {planar_count} planar + {cyl_count} cylindrical faces. "
            f"Base body is a rectangular block ({dx:.2f} × {dy:.2f} × {dz:.2f} mm). "
            f"Do NOT use a circle for the base sketch. Use sketch_rectangle."
        )
    elif cyl_count >= planar_count and planar_count <= 2:
        body_shape = (
            f"CYLINDRICAL BODY — {cyl_count} cylindrical faces, {planar_count} planar faces. "
            f"Model as sketch_circle, revolve_boss, or extrude of circle profile."
        )
    else:
        body_shape = f"MIXED — {planar_count} planar, {cyl_count} cylindrical, {tor_count} toroidal faces."

    # Augment detected features with computed face_point hints for holes
    features_with_hints = _augment_hole_face_points(geometry.detected_features, bb_min, bb_max)
    features_json = json.dumps(features_with_hints, indent=2) if features_with_hints else "[]"

    return f"""\
Analyze the following part geometry and produce a reconstruction plan.

## Part Dimensions
- File: {geometry.file_name}
- Bounding box: ({bb_min.x:.3f}, {bb_min.y:.3f}, {bb_min.z:.3f}) to ({bb_max.x:.3f}, {bb_max.y:.3f}, {bb_max.z:.3f}) mm
- Dimensions: {dx:.3f} mm (X) × {dy:.3f} mm (Y) × {dz:.3f} mm (Z)
- Volume: {geometry.volume:.3f} mm³
- Surface area: {geometry.surface_area:.3f} mm²
- Center of mass: ({com.x:.3f}, {com.y:.3f}, {com.z:.3f}) mm

## Body Shape
{body_shape}

## Detected Features
{features_json}
Note: "suggested_face_point" on each hole is a reliable point on the correct face for hole_wizard's face_point parameter.
      Use this value directly — do not invent your own face_point.

## Face Summary
- Total faces: {len(geometry.faces)}
- Planar: {planar_count}  (unique normals: {unique_normals})
- Cylindrical: {cyl_count}  (radii mm: {unique_radii})
- Toroidal (fillets): {tor_count}  (minor radii mm: {unique_fillet_radii})
- Other: {other_face_count}

## Symmetry
{symmetry_info}

## Edge Summary
- Total edges: {len(geometry.edges)}
- Linear: {linear_count}
- Circular (circles + arcs): {circular_count}
- Other: {other_edge_count}

Produce the reconstruction plan as JSON.
"""


def _augment_hole_face_points(features: list, bb_min, bb_max) -> list:
    """
    For each hole feature, compute a 'suggested_face_point' — a point that lies
    cleanly on the center of the entry face, derived from the bounding box and
    hole axis. This is more reliable than letting the LLM guess coordinates.
    """
    result = []
    for feat in features:
        f = dict(feat)
        if feat.get("type") in ("through_hole", "blind_hole") and "axis" in feat and "center" in feat:
            ax = feat["axis"]
            cx, cy, cz = feat["center"]
            xmid = (bb_min.x + bb_max.x) / 2
            ymid = (bb_min.y + bb_max.y) / 2
            zmid = (bb_min.z + bb_max.z) / 2

            if abs(ax[0]) >= 0.7:      # hole axis ≈ X
                # entry face: +X or -X wall; face center is at that X, mid Y and Z
                entry_x = bb_min.x if cx <= xmid else bb_max.x
                f["suggested_face_point"] = [round(entry_x, 3), round(ymid, 3), round(zmid, 3)]
            elif abs(ax[1]) >= 0.7:    # hole axis ≈ Y
                # entry face: +Y or -Y wall; use bottom face (bb_min.y)
                # position on face: hole's X, bottom Y, face Z-center
                f["suggested_face_point"] = [round(cx, 3), round(bb_min.y, 3), round(zmid, 3)]
            else:                       # hole axis ≈ Z
                # entry face: +Z or -Z wall
                entry_z = bb_min.z if cz <= zmid else bb_max.z
                f["suggested_face_point"] = [round(xmid, 3), round(ymid, 3), round(entry_z, 3)]
        result.append(f)
    return result


def _unique_vectors(vectors) -> list:
    """Return deduplicated list of vector tuples (rounded to 2 dp)."""
    seen = set()
    result = []
    for v in vectors:
        key = (round(v.x, 2), round(v.y, 2), round(v.z, 2))
        if key not in seen:
            seen.add(key)
            result.append(list(key))
    return result
