"""
Higher-level feature recognition: turns raw faces/edges into semantic features
(holes, fillets, chamfers, patterns, symmetry) before sending to Claude.
"""

import logging
import math
from collections import defaultdict
from typing import Optional

from models.geometry import Edge, EdgeType, Face, FaceType, PartGeometry, Vector3D

logger = logging.getLogger(__name__)

# Tolerance for floating-point comparisons (mm)
_TOL = 0.01
# Angle tolerance (radians) for "parallel" or "perpendicular" checks
_ANGLE_TOL = math.radians(2.0)
# Chamfer face: a planar face whose normal is within this angle of 45 deg from adjacent faces
_CHAMFER_ANGLE_MIN = math.radians(30.0)
_CHAMFER_ANGLE_MAX = math.radians(60.0)


class FeatureRecognizer:
    """
    Analyses a PartGeometry (populated by StepExtractor) and appends
    high-level detected features to geometry.detected_features.
    """

    def __init__(self, faces: list[Face], edges: list[Edge], geometry: PartGeometry):
        self.faces = faces
        self.edges = edges
        self.geometry = geometry

    def recognize(self) -> list[dict]:
        """Run all detectors and return the combined feature list."""
        features: list[dict] = []
        features.extend(self._detect_holes())
        features.extend(self._detect_fillets())
        features.extend(self._detect_chamfers())
        features.extend(self._detect_linear_patterns(features))
        features.extend(self._detect_circular_patterns(features))
        features.extend(self._detect_symmetry_features())
        features.extend(self._detect_polygon_pockets())

        logger.info("Feature recognizer found %d high-level features.", len(features))
        self.geometry.detected_features = features
        return features

    # ------------------------------------------------------------------
    # Hole detection (through and blind, combined)
    # ------------------------------------------------------------------

    def _detect_holes(self) -> list[dict]:
        """
        Detect through-holes and blind holes robustly using Edge.adjacent_face_ids.

        The old cap-counting approach broke on flat discs because both the outer
        rim AND interior holes are adjacent to the same top/bottom faces, making
        cap_count always ≥ 2 and preventing any classification.

        New strategy
        ────────────
        1. **Radius filter**: skip any cylindrical group whose radius is ≥ 45 % of
           the largest bounding-box dimension — that cylinder defines the outer
           shape of the part, not a hole.
        2. **Find the two main outer faces** (largest planar faces with normal
           along the dominant base axis) and mark them as "outer_face_ids".
        3. **Adjacent planar faces** for each cylinder group are found via
           Edge.adjacent_face_ids (populated by StepExtractor).
        4. If any adjacent planar face is NOT an outer face AND its normal is
           parallel to the cylinder axis → it is a pocket floor → **blind hole**.
           Otherwise → **through hole**.
        """
        bb_min = self.geometry.bounding_box_min
        bb_max = self.geometry.bounding_box_max
        max_extent = max(
            bb_max.x - bb_min.x,
            bb_max.y - bb_min.y,
            bb_max.z - bb_min.z,
        )

        # Identify the two outer (top/bottom) planar faces
        planar_faces = [f for f in self.faces if f.face_type == FaceType.PLANAR and f.normal]
        outer_face_ids: set[int] = set()
        if planar_faces:
            weights = {"top": 0.0, "front": 0.0, "right": 0.0}
            for f in planar_faces:
                n = f.normal
                mag = math.sqrt(n.x ** 2 + n.y ** 2 + n.z ** 2)
                if mag < 1e-10:
                    continue
                weights["top"]   += f.area * abs(n.z) / mag
                weights["front"] += f.area * abs(n.y) / mag
                weights["right"] += f.area * abs(n.x) / mag
            base_axis_vec = {
                "top":   Vector3D(0, 0, 1),
                "front": Vector3D(0, 1, 0),
                "right": Vector3D(1, 0, 0),
            }[max(weights, key=lambda k: weights[k])]
            base_plane_faces = [
                f for f in planar_faces
                if _normals_parallel(f.normal, base_axis_vec, _ANGLE_TOL)
            ]
            base_plane_faces.sort(key=lambda f: f.area, reverse=True)
            for f in base_plane_faces[:2]:
                outer_face_ids.add(f.id)

        face_by_id = {f.id: f for f in self.faces}

        def _adj_planar_non_outer(cyl_ids: set[int]) -> list[Face]:
            """Return planar faces adjacent to this cylinder group that are NOT outer faces."""
            seen: set[int] = set()
            result: list[Face] = []
            for e in self.edges:
                if not any(fid in e.adjacent_face_ids for fid in cyl_ids):
                    continue
                for fid in e.adjacent_face_ids:
                    if fid in seen or fid in cyl_ids:
                        continue
                    seen.add(fid)
                    f = face_by_id.get(fid)
                    if f and f.face_type == FaceType.PLANAR and f.id not in outer_face_ids:
                        result.append(f)
            return result

        cyl_faces = [
            f for f in self.faces
            if f.face_type == FaceType.CYLINDRICAL and f.center and f.axis
        ]
        if not cyl_faces:
            return []

        groups = _group_coaxial_cylinders(cyl_faces)
        features: list[dict] = []
        through_id = 1
        blind_id   = 1

        for key, group in groups.items():
            radius, axis = key

            # Skip the outer-boundary cylinder(s)
            if max_extent > 0 and radius / max_extent >= 0.45:
                continue

            rep    = group[0]
            cyl_ids = {f.id for f in group}

            # Find non-outer adjacent planar faces (potential hole floors)
            floor_candidates = _adj_planar_non_outer(cyl_ids)

            # A floor must have its normal parallel to the hole axis
            floor_faces = [
                f for f in floor_candidates
                if f.normal and _normals_parallel(f.normal, Vector3D(*axis), _ANGLE_TOL)
            ]

            if floor_faces:
                depth = _estimate_cylinder_depth(group, Vector3D(*axis))
                features.append({
                    "type":     "blind_hole",
                    "id":       f"blind_hole_{blind_id}",
                    "diameter": round(radius * 2, 4),
                    "center":   [round(v, 4) for v in rep.center.to_list()],
                    "axis":     [round(v, 4) for v in axis],
                    "depth":    round(depth, 4),
                    "face_ids": [f.id for f in group],
                })
                blind_id += 1
            else:
                features.append({
                    "type":     "through_hole",
                    "id":       f"through_hole_{through_id}",
                    "diameter": round(radius * 2, 4),
                    "center":   [round(v, 4) for v in rep.center.to_list()],
                    "axis":     [round(v, 4) for v in axis],
                    "depth":    "through_all",
                    "face_ids": [f.id for f in group],
                })
                through_id += 1

        return features

    # ------------------------------------------------------------------
    # Fillet / chamfer detection
    # ------------------------------------------------------------------

    def _detect_fillets(self) -> list[dict]:
        """
        Fillets: toroidal faces (FaceType.TOROIDAL).
        Group by minor_radius (the tube radius = fillet radius).
        """
        tor_faces = [f for f in self.faces if f.face_type == FaceType.TOROIDAL and f.minor_radius]
        if not tor_faces:
            return []

        # Group by minor_radius
        radius_groups: dict[float, list[Face]] = defaultdict(list)
        for f in tor_faces:
            key = round(f.minor_radius, 3)
            radius_groups[key].append(f)

        features = []
        for radius, group in radius_groups.items():
            features.append(
                {
                    "type": "fillet",
                    "radius": radius,
                    "face_count": len(group),
                    "face_ids": [f.id for f in group],
                }
            )
        return features

    def _detect_chamfers(self) -> list[dict]:
        """
        Chamfers: small planar faces whose normal is at ~45° to at least two adjacent planar faces.
        """
        planar_faces = [f for f in self.faces if f.face_type == FaceType.PLANAR and f.normal]
        face_by_id = {f.id: f for f in self.faces}

        chamfer_faces = []
        for f in planar_faces:
            if f.area > self._median_face_area(planar_faces) * 0.1:
                # Too large to be a chamfer face
                continue
            adj_planar = [
                face_by_id[i]
                for i in f.adjacent_face_ids
                if i in face_by_id and face_by_id[i].face_type == FaceType.PLANAR and face_by_id[i].normal
            ]
            for adj in adj_planar:
                angle = _angle_between(f.normal, adj.normal)
                if _CHAMFER_ANGLE_MIN <= angle <= _CHAMFER_ANGLE_MAX:
                    chamfer_faces.append(f)
                    break

        if not chamfer_faces:
            return []

        return [
            {
                "type": "chamfer",
                "face_count": len(chamfer_faces),
                "face_ids": [f.id for f in chamfer_faces],
            }
        ]

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def _detect_linear_patterns(self, existing_features: list[dict]) -> list[dict]:
        """
        Detect linear patterns among through-holes and blind holes.
        Look for groups of same-diameter holes whose centers are collinear
        and evenly spaced.
        """
        hole_features = [
            f for f in existing_features if f["type"] in {"through_hole", "blind_hole"}
        ]
        if len(hole_features) < 2:
            return []

        used: set[str] = set()
        patterns = []
        pat_id = 1

        for i, h1 in enumerate(hole_features):
            if h1["id"] in used:
                continue
            d1 = h1["diameter"]
            c1 = Vector3D(*h1["center"])

            for j, h2 in enumerate(hole_features):
                if j <= i or h2["id"] in used:
                    continue
                if abs(h2["diameter"] - d1) > _TOL:
                    continue

                c2 = Vector3D(*h2["center"])
                direction = _normalize(_subtract(c2, c1))
                spacing = _distance(c1, c2)

                # Collect all holes collinear with c1→c2 and evenly spaced
                collinear = [h1, h2]
                for h3 in hole_features:
                    if h3["id"] in {h1["id"], h2["id"]} or abs(h3["diameter"] - d1) > _TOL:
                        continue
                    c3 = Vector3D(*h3["center"])
                    if _point_on_line(c3, c1, direction) and _is_multiple_of(
                        _distance(c1, c3), spacing, _TOL
                    ):
                        collinear.append(h3)

                if len(collinear) >= 3:
                    ids = {h["id"] for h in collinear}
                    if ids not in [set(p.get("_ids", [])) for p in patterns]:
                        for h in collinear:
                            used.add(h["id"])
                        patterns.append(
                            {
                                "type": "linear_pattern",
                                "id": f"linear_pattern_{pat_id}",
                                "feature_type": h1["type"],
                                "diameter": round(d1, 4),
                                "count": len(collinear),
                                "spacing": round(spacing, 4),
                                "direction": [round(v, 4) for v in direction],
                                "feature_ids": [h["id"] for h in collinear],
                                "_ids": list(ids),
                            }
                        )
                        pat_id += 1

        return patterns

    def _detect_circular_patterns(self, existing_features: list[dict]) -> list[dict]:
        """
        Detect circular patterns: same-diameter holes arranged at uniform
        angular spacing around a common axis.
        """
        hole_features = [
            f for f in existing_features if f["type"] in {"through_hole", "blind_hole"}
        ]
        if len(hole_features) < 3:
            return []

        used: set[str] = set()
        patterns = []
        pat_id = 1

        for i, h1 in enumerate(hole_features):
            if h1["id"] in used:
                continue
            d1 = h1["diameter"]
            c1 = Vector3D(*h1["center"])
            ax1 = tuple(h1["axis"])

            same_type = [
                h
                for h in hole_features
                if abs(h["diameter"] - d1) < _TOL and tuple(h["axis"]) == ax1
            ]
            if len(same_type) < 3:
                continue

            # Compute radii from part center-of-mass projected onto plane ⊥ axis
            com = self.geometry.center_of_mass
            radii = []
            for h in same_type:
                c = Vector3D(*h["center"])
                r = _distance_to_axis(c, com, Vector3D(*ax1))
                radii.append(r)

            avg_r = sum(radii) / len(radii)
            if all(abs(r - avg_r) < avg_r * 0.05 for r in radii):
                ids = {h["id"] for h in same_type}
                if ids not in [set(p.get("_ids", [])) for p in patterns]:
                    for h in same_type:
                        used.add(h["id"])
                    angular_spacing = round(360.0 / len(same_type), 2)
                    patterns.append(
                        {
                            "type": "circular_pattern",
                            "id": f"circular_pattern_{pat_id}",
                            "feature_type": h1["type"],
                            "diameter": round(d1, 4),
                            "count": len(same_type),
                            "angular_spacing": angular_spacing,
                            "axis": list(ax1),
                            "bolt_circle_radius": round(avg_r, 4),
                            "feature_ids": [h["id"] for h in same_type],
                            "_ids": list(ids),
                        }
                    )
                    pat_id += 1

        return patterns

    # ------------------------------------------------------------------
    # Symmetry features
    # ------------------------------------------------------------------

    def _detect_symmetry_features(self) -> list[dict]:
        """Wrap the symmetry planes already detected by StepExtractor."""
        return [
            {"type": "symmetry", "plane": plane}
            for plane in self.geometry.symmetry_planes
        ]

    # ------------------------------------------------------------------
    # Polygon pockets (rectangular, triangular, etc.)
    # ------------------------------------------------------------------

    def _detect_polygon_pockets(self) -> list[dict]:
        """
        Detect flat-bottomed pockets whose profiles are made of LINE edges
        (rectangles, triangles, slots, etc.).  Circular pockets are already
        handled by _detect_blind_holes via their cylindrical walls.

        Requires adjacent_face_ids to be populated on edges (by StepExtractor).
        A pocket floor is a planar face whose outward normal points in the
        same direction as the part's base-face normal, but at a recessed
        position along that axis.
        """
        planar_faces = [f for f in self.faces if f.face_type == FaceType.PLANAR and f.normal]
        if not planar_faces:
            return []

        # Determine the dominant base direction (same voting as AlgorithmicPlanner)
        weights = {"top": 0.0, "front": 0.0, "right": 0.0}
        for f in planar_faces:
            n = f.normal
            mag = math.sqrt(n.x ** 2 + n.y ** 2 + n.z ** 2)
            if mag < 1e-10:
                continue
            weights["top"]   += f.area * abs(n.z) / mag
            weights["front"] += f.area * abs(n.y) / mag
            weights["right"] += f.area * abs(n.x) / mag

        base_dir  = max(weights, key=lambda k: weights[k])
        base_axis = {"top": Vector3D(0, 0, 1),
                     "front": Vector3D(0, 1, 0),
                     "right": Vector3D(1, 0, 0)}[base_dir]

        bb_min = self.geometry.bounding_box_min
        bb_max = self.geometry.bounding_box_max
        outer_pos = {"top": bb_max.z, "front": bb_max.y, "right": bb_max.x}[base_dir]

        # Tolerance: 2 % of part extent along base axis
        extent = {"top":   bb_max.z - bb_min.z,
                  "front": bb_max.y - bb_min.y,
                  "right": bb_max.x - bb_min.x}[base_dir]
        pos_tol = max(extent * 0.02, _TOL)

        def _face_axis_pos(face_id: int) -> Optional[float]:
            """Estimate face position along the base axis from adjacent edge endpoints."""
            coords: list[float] = []
            for e in self.edges:
                if face_id not in e.adjacent_face_ids:
                    continue
                if base_dir == "top":
                    coords += [e.start_point.z, e.end_point.z]
                elif base_dir == "front":
                    coords += [e.start_point.y, e.end_point.y]
                else:
                    coords += [e.start_point.x, e.end_point.x]
            return sum(coords) / len(coords) if coords else None

        features: list[dict] = []
        pocket_id = 1

        for f in planar_faces:
            # Normal must point in the SAME direction as base_axis (outward top face)
            dot = (f.normal.x * base_axis.x
                   + f.normal.y * base_axis.y
                   + f.normal.z * base_axis.z)
            if dot < 0.8:
                continue  # wall or bottom face — skip

            pos = _face_axis_pos(f.id)
            if pos is None:
                continue  # no adjacent edges → adjacency not populated, skip

            # Must be recessed relative to the outer surface
            if abs(pos - outer_pos) < pos_tol:
                continue  # this IS the main outer top face
            depth = round(outer_pos - pos, 4)
            if depth <= pos_tol:
                continue

            # Collect boundary edges that are LINE type, then extract the
            # largest closed loop (filters degenerate and disconnected edges).
            floor_edges = [e for e in self.edges if f.id in e.adjacent_face_ids]
            line_edges  = [e for e in floor_edges if e.edge_type == EdgeType.LINE]
            profile_edges = _closed_loop_edges(line_edges, base_dir)

            if len(profile_edges) < 3:
                continue  # need at least a triangle; circles → handled by blind_hole

            features.append({
                "type":         "polygon_pocket",
                "id":           f"polygon_pocket_{pocket_id}",
                "face_id":      f.id,
                "base_dir":     base_dir,
                "depth":        depth,
                "edge_ids":     [e.id for e in profile_edges],
                "vertex_count": len(profile_edges),
            })
            pocket_id += 1

        return features

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _median_face_area(faces: list[Face]) -> float:
        if not faces:
            return 1.0
        areas = sorted(f.area for f in faces)
        mid = len(areas) // 2
        return areas[mid]


# ------------------------------------------------------------------
# Closed-loop edge extraction
# ------------------------------------------------------------------

def _closed_loop_edges(edges: list[Edge], base_dir: str) -> list[Edge]:
    """
    From a set of LINE edges, return the edges that form the largest closed
    polygon loop in the sketch projection.

    Filters:
    • Degenerate edges — projected start ≈ projected end (zero 2-D length).
    • Disconnected edges — not part of a closed loop (all nodes degree == 2).

    Falls back to returning all non-degenerate edges if no clean loop is found
    (e.g. open contour due to tolerance issues).
    """
    _SNAP = 0.5   # mm — point-snapping tolerance for endpoint matching

    def proj2d(v: Vector3D) -> tuple[float, float]:
        if base_dir == "top":
            return (v.x, v.y)
        elif base_dir == "front":
            return (v.x, v.z)
        else:
            return (v.y, v.z)

    def snap(pt: tuple[float, float]) -> tuple[float, float]:
        return (round(pt[0] / _SNAP) * _SNAP, round(pt[1] / _SNAP) * _SNAP)

    # Step 1: filter degenerate (zero-length when projected)
    valid: list[tuple[Edge, tuple, tuple]] = []
    for e in edges:
        s = proj2d(e.start_point)
        t = proj2d(e.end_point)
        if math.sqrt((s[0] - t[0]) ** 2 + (s[1] - t[1]) ** 2) > _SNAP:
            valid.append((e, snap(s), snap(t)))

    if not valid:
        return []

    # Step 2: build adjacency graph (snapped endpoint → edge indices)
    node_edges: dict[tuple, list[int]] = defaultdict(list)
    for i, (_, s, t) in enumerate(valid):
        node_edges[s].append(i)
        node_edges[t].append(i)

    # Step 3: find connected components via DFS
    visited: set[int] = set()

    def component(start: int) -> list[int]:
        comp: list[int] = []
        stack = [start]
        while stack:
            idx = stack.pop()
            if idx in visited:
                continue
            visited.add(idx)
            comp.append(idx)
            _, s, t = valid[idx]
            for nb in node_edges[s] + node_edges[t]:
                if nb not in visited:
                    stack.append(nb)
        return comp

    components: list[list[int]] = []
    for i in range(len(valid)):
        if i not in visited:
            components.append(component(i))

    # Step 4: keep only components that form a proper closed loop (all deg == 2)
    closed: list[list[int]] = []
    for comp in components:
        deg: dict[tuple, int] = defaultdict(int)
        for idx in comp:
            _, s, t = valid[idx]
            deg[s] += 1
            deg[t] += 1
        if all(d == 2 for d in deg.values()):
            closed.append(comp)

    if closed:
        best = max(closed, key=len)
        return [valid[i][0] for i in best]

    # Fallback: largest component (open contour)
    if components:
        best = max(components, key=len)
        return [valid[i][0] for i in best]

    return []


# ------------------------------------------------------------------
# Math helpers
# ------------------------------------------------------------------

def _group_coaxial_cylinders(
    cyl_faces: list[Face],
) -> dict[tuple, list[Face]]:
    """Group cylindrical faces by (rounded_radius, normalized_axis_tuple)."""
    groups: dict[tuple, list[Face]] = defaultdict(list)
    for f in cyl_faces:
        r = round(f.radius, 3)
        ax = _normalize_axis(f.axis)
        groups[(r, ax)].append(f)
    return groups


def _normalize_axis(v: Vector3D) -> tuple:
    """Return a canonical (always positive Z or X or Y component first) unit axis tuple."""
    mag = math.sqrt(v.x**2 + v.y**2 + v.z**2)
    if mag < 1e-10:
        return (0.0, 0.0, 1.0)
    nx, ny, nz = v.x / mag, v.y / mag, v.z / mag
    # Canonicalize sign: flip so that the first nonzero component is positive
    for c in (nz, ny, nx):
        if abs(c) > 1e-6:
            if c < 0:
                nx, ny, nz = -nx, -ny, -nz
            break
    return (round(nx, 4), round(ny, 4), round(nz, 4))


def _normals_parallel(n1: Optional[Vector3D], n2: Vector3D, tol: float) -> bool:
    if n1 is None:
        return False
    return _angle_between(n1, n2) < tol


def _angle_between(a: Vector3D, b: Vector3D) -> float:
    dot = _dot(a, b)
    ma = math.sqrt(a.x**2 + a.y**2 + a.z**2)
    mb = math.sqrt(b.x**2 + b.y**2 + b.z**2)
    if ma < 1e-10 or mb < 1e-10:
        return 0.0
    cos_a = max(-1.0, min(1.0, dot / (ma * mb)))
    angle = math.acos(cos_a)
    return min(angle, math.pi - angle)  # always return the acute angle


def _dot(a: Vector3D, b: Vector3D) -> float:
    return a.x * b.x + a.y * b.y + a.z * b.z


def _subtract(a: Vector3D, b: Vector3D) -> Vector3D:
    return Vector3D(a.x - b.x, a.y - b.y, a.z - b.z)


def _distance(a: Vector3D, b: Vector3D) -> float:
    d = _subtract(a, b)
    return math.sqrt(d.x**2 + d.y**2 + d.z**2)


def _normalize(v: Vector3D) -> tuple:
    mag = math.sqrt(v.x**2 + v.y**2 + v.z**2)
    if mag < 1e-10:
        return (0.0, 0.0, 1.0)
    return (v.x / mag, v.y / mag, v.z / mag)


def _point_on_line(p: Vector3D, origin: Vector3D, direction: tuple, tol: float = _TOL) -> bool:
    """Return True if p lies on the line through origin in direction."""
    op = _subtract(p, origin)
    # Cross product magnitude: |op × direction| should be ~0
    dx, dy, dz = direction
    cx = op.y * dz - op.z * dy
    cy = op.z * dx - op.x * dz
    cz = op.x * dy - op.y * dx
    cross_mag = math.sqrt(cx**2 + cy**2 + cz**2)
    return cross_mag < tol


def _is_multiple_of(value: float, unit: float, tol: float) -> bool:
    if unit < tol:
        return False
    ratio = value / unit
    return abs(ratio - round(ratio)) < tol / unit


def _distance_to_axis(point: Vector3D, axis_origin: Vector3D, axis_dir: Vector3D) -> float:
    """Perpendicular distance from point to an infinite axis."""
    op = _subtract(point, axis_origin)
    d_dot = _dot(op, axis_dir)
    mag2 = axis_dir.x**2 + axis_dir.y**2 + axis_dir.z**2
    if mag2 < 1e-10:
        return _distance(point, axis_origin)
    proj = Vector3D(
        op.x - d_dot * axis_dir.x / mag2,
        op.y - d_dot * axis_dir.y / mag2,
        op.z - d_dot * axis_dir.z / mag2,
    )
    return math.sqrt(proj.x**2 + proj.y**2 + proj.z**2)


def _estimate_cylinder_depth(group: list[Face], axis: Vector3D) -> float:
    """Estimate depth of a cylindrical feature by projecting centers along axis."""
    if not group:
        return 0.0
    projections = [_dot(f.center, axis) for f in group if f.center]
    if not projections:
        return 0.0
    return max(projections) - min(projections)


def _is_interior_cylinder(
    center: Vector3D,
    axis: tuple,
    radius: float,
    bb_min: Vector3D,
    bb_max: Vector3D,
    threshold: float = 0.25,
) -> bool:
    """
    Return True if the cylinder center is interior to the bounding box in the
    directions perpendicular to the axis.

    A genuine hole's center must sit at least `radius * threshold` inside the
    bbox on every perpendicular axis.  Centers on the bbox boundary indicate
    surface features (edge fillets, rounded corners) rather than drilled holes.
    """
    margin = radius * threshold
    ax = axis  # already a normalized tuple from _normalize_axis

    # For each world axis, if the cylinder axis has a small component in that
    # direction (i.e. the world axis is mostly perpendicular to the hole axis),
    # the hole center must be well inside the bbox along that world axis.
    checks = [
        (abs(ax[0]) < 0.5, center.x, bb_min.x, bb_max.x),
        (abs(ax[1]) < 0.5, center.y, bb_min.y, bb_max.y),
        (abs(ax[2]) < 0.5, center.z, bb_min.z, bb_max.z),
    ]
    for is_perp, c, lo, hi in checks:
        if is_perp and (c < lo + margin or c > hi - margin):
            return False
    return True
