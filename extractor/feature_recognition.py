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
        features.extend(self._detect_through_holes())
        features.extend(self._detect_blind_holes())
        features.extend(self._detect_fillets())
        features.extend(self._detect_chamfers())
        features.extend(self._detect_linear_patterns(features))
        features.extend(self._detect_circular_patterns(features))
        features.extend(self._detect_symmetry_features())
        features.extend(self._detect_pockets_and_bosses())

        logger.info("Feature recognizer found %d high-level features.", len(features))
        self.geometry.detected_features = features
        return features

    # ------------------------------------------------------------------
    # Hole detection
    # ------------------------------------------------------------------

    def _detect_through_holes(self) -> list[dict]:
        """
        Through-holes: cylindrical face(s) that are open on both ends.
        We group cylindrical faces by (radius, axis_direction) and check
        that the group has no planar cap at one end.
        """
        cyl_faces = [f for f in self.faces if f.face_type == FaceType.CYLINDRICAL and f.center and f.axis]
        if not cyl_faces:
            return []

        groups = _group_coaxial_cylinders(cyl_faces)
        features = []
        hole_id = 1

        for key, group in groups.items():
            radius, axis = key
            # Check adjacent planar faces for caps
            all_adj_ids = set()
            for f in group:
                all_adj_ids.update(f.adjacent_face_ids)

            adj_planar = [
                self.faces[i]
                for i in all_adj_ids
                if i < len(self.faces) and self.faces[i].face_type == FaceType.PLANAR
            ]

            # Through-hole: 0 or very few planar caps relative to the expected 2
            # (a single cylinder may share its open ends with the outer body)
            cap_count = sum(
                1 for pf in adj_planar if _normals_parallel(pf.normal, Vector3D(*axis), _ANGLE_TOL)
            )

            if cap_count < 2:
                rep = group[0]
                features.append(
                    {
                        "type": "through_hole",
                        "id": f"through_hole_{hole_id}",
                        "diameter": round(radius * 2, 4),
                        "center": [round(v, 4) for v in rep.center.to_list()],
                        "axis": [round(v, 4) for v in axis],
                        "depth": "through_all",
                        "face_ids": [f.id for f in group],
                    }
                )
                hole_id += 1

        return features

    def _detect_blind_holes(self) -> list[dict]:
        """
        Blind holes: cylindrical face with at least one planar cap (bottom)
        and one open end (no cap on the other side).
        """
        cyl_faces = [f for f in self.faces if f.face_type == FaceType.CYLINDRICAL and f.center and f.axis]
        if not cyl_faces:
            return []

        groups = _group_coaxial_cylinders(cyl_faces)
        features = []
        hole_id = 1

        for key, group in groups.items():
            radius, axis = key
            all_adj_ids = set()
            for f in group:
                all_adj_ids.update(f.adjacent_face_ids)

            adj_planar = [
                self.faces[i]
                for i in all_adj_ids
                if i < len(self.faces) and self.faces[i].face_type == FaceType.PLANAR
            ]

            cap_count = sum(
                1 for pf in adj_planar if _normals_parallel(pf.normal, Vector3D(*axis), _ANGLE_TOL)
            )

            # Exactly 1 cap → blind hole
            if cap_count == 1:
                rep = group[0]
                # Estimate depth from bounding extent along axis
                depth = _estimate_cylinder_depth(group, Vector3D(*axis))
                features.append(
                    {
                        "type": "blind_hole",
                        "id": f"blind_hole_{hole_id}",
                        "diameter": round(radius * 2, 4),
                        "center": [round(v, 4) for v in rep.center.to_list()],
                        "axis": [round(v, 4) for v in axis],
                        "depth": round(depth, 4),
                        "face_ids": [f.id for f in group],
                    }
                )
                hole_id += 1

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
    # Pockets and bosses
    # ------------------------------------------------------------------

    def _detect_pockets_and_bosses(self) -> list[dict]:
        """
        Heuristic detection of pockets (depressions) and bosses (protrusions).
        A group of planar faces forming a closed depression below the main body
        surface is a pocket; a group forming a protrusion above is a boss.

        This is a simplified heuristic: compare face normal directions relative
        to the part bounding box to classify inward vs outward facing groups.
        """
        features = []
        bb_min = self.geometry.bounding_box_min
        bb_max = self.geometry.bounding_box_max
        part_center = Vector3D(
            (bb_min.x + bb_max.x) / 2,
            (bb_min.y + bb_max.y) / 2,
            (bb_min.z + bb_max.z) / 2,
        )

        # Identify "large" planar faces (likely outer walls / top/bottom surfaces)
        planar_faces = [f for f in self.faces if f.face_type == FaceType.PLANAR and f.normal]
        if not planar_faces:
            return features

        median_area = self._median_face_area(planar_faces)

        # Small planar faces with inward-pointing normals are likely pocket walls/floors
        pocket_candidates = []
        boss_candidates = []

        for f in planar_faces:
            if f.area >= median_area * 0.5:
                continue  # too large to be a pocket/boss wall
            # The face center is roughly at the face's center-of-mass
            # (we don't have it directly, but we can use adjacent geometry)
            # Simple heuristic: check if the normal points toward or away from part center
            if f.center:
                to_center = _subtract(part_center, f.center)
                dot = _dot(f.normal, to_center)
                if dot > 0:
                    pocket_candidates.append(f)
                else:
                    boss_candidates.append(f)

        if pocket_candidates:
            features.append(
                {
                    "type": "pocket",
                    "face_count": len(pocket_candidates),
                    "face_ids": [f.id for f in pocket_candidates],
                    "note": "heuristic — verify in CAD viewer",
                }
            )
        if boss_candidates:
            features.append(
                {
                    "type": "boss",
                    "face_count": len(boss_candidates),
                    "face_ids": [f.id for f in boss_candidates],
                    "note": "heuristic — verify in CAD viewer",
                }
            )

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


from typing import Optional  # noqa: E402 (already imported above, harmless)
