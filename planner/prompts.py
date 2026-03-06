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
- Use sketch planes and references that make geometric sense (not arbitrary)
- Prefer symmetric sketches centered on the origin when the part has symmetry
- Use patterns (linear, circular) instead of repeating individual features
- Use mirror features when the part has mirror symmetry
- Add fillets and chamfers LAST (they depend on edges from earlier features)
- Add holes after the main body is established
- Dimension sketches fully — no under-constrained geometry
- Name every feature descriptively

OUTPUT FORMAT:
Return a JSON object matching this schema exactly:
{
  "summary": "Brief description of the part",
  "base_plane": "front" | "top" | "right",
  "modeling_strategy": "Explanation of your approach and why",
  "operations": [
    {
      "step_number": 1,
      "operation_type": "<operation enum value>",
      "parameters": { },
      "description": "Human-readable description",
      "references": ["names of prior features this depends on"]
    }
  ],
  "notes": ["Any caveats or alternative approaches"]
}

VALID OPERATION TYPES:
new_sketch, sketch_line, sketch_rectangle, sketch_circle, sketch_arc,
sketch_dimension, sketch_constraint, close_sketch,
extrude_boss, extrude_cut, revolve_boss, revolve_cut,
fillet, chamfer, hole_wizard, linear_pattern, circular_pattern, mirror, shell

PARAMETER SCHEMAS BY OPERATION TYPE:

new_sketch:
  plane: "front" | "top" | "right" | {"type": "face_ref", "feature": "...", "face_index": 0}
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
  position: [x, y]  on sketch plane
  standard: "ANSI Metric" | "ANSI Inch"  (optional)

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

    features_json = json.dumps(geometry.detected_features, indent=2) if geometry.detected_features else "[]"

    return f"""\
Analyze the following part geometry and produce a reconstruction plan.

## Part Information
- File: {geometry.file_name}
- Bounding box: ({bb_min.x:.3f}, {bb_min.y:.3f}, {bb_min.z:.3f}) to ({bb_max.x:.3f}, {bb_max.y:.3f}, {bb_max.z:.3f}) mm
- Volume: {geometry.volume:.3f} mm³
- Surface area: {geometry.surface_area:.3f} mm²
- Center of mass: ({com.x:.3f}, {com.y:.3f}, {com.z:.3f}) mm

## Detected Features
{features_json}

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
