"""
STEP file geometry extractor using pythonOCC (OpenCascade).

Install pythonOCC via conda:
    conda install -c conda-forge pythonocc-core
"""

import logging
import math
from pathlib import Path

from models.geometry import (
    Edge,
    EdgeType,
    Face,
    FaceType,
    PartGeometry,
    Vector3D,
)

logger = logging.getLogger(__name__)

try:
    from OCC.Core.STEPControl import STEPControl_Reader
    from OCC.Core.IFSelect import IFSelect_RetDone
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_EDGE
    from OCC.Core.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
    from OCC.Core.GeomAbs import (
        GeomAbs_Plane,
        GeomAbs_Cylinder,
        GeomAbs_Cone,
        GeomAbs_Sphere,
        GeomAbs_Torus,
        GeomAbs_BSplineSurface,
        GeomAbs_Line,
        GeomAbs_Circle,
        GeomAbs_Ellipse,
        GeomAbs_BSplineCurve,
    )
    from OCC.Core.GProp import GProp_GProps
    from OCC.Core.Bnd import Bnd_Box
    # BRepGProp: function names differ between pythonocc versions — try both
    try:
        from OCC.Core.BRepGProp import brepgprop_VolumeProperties, brepgprop_SurfaceProperties
    except ImportError:
        from OCC.Core.BRepGProp import BRepGProp_VolumeProperties as brepgprop_VolumeProperties, \
            BRepGProp_SurfaceProperties as brepgprop_SurfaceProperties
    # BRepBndLib: same issue
    try:
        from OCC.Core.BRepBndLib import brepbndlib_Add
    except ImportError:
        from OCC.Core.BRepBndLib import BRepBndLib_Add as brepbndlib_Add
    from OCC.Core.BRep import BRep_Tool
    from OCC.Core.TopoDS import topods  # topods.Face(), topods.Edge() in pythonocc 7.7+
    from OCC.Core.TopTools import (
        TopTools_IndexedDataMapOfShapeListOfShape,
        TopTools_IndexedMapOfShape,
    )
    from OCC.Core.TopExp import topexp_MapShapesAndAncestors

    _OCC_AVAILABLE = True
except Exception as _occ_import_error:
    _OCC_AVAILABLE = False
    logger.warning(
        "pythonOCC import failed: %s\n"
        "Install via: conda install -c conda-forge pythonocc-core",
        _occ_import_error,
    )


def _require_occ():
    if not _OCC_AVAILABLE:
        raise RuntimeError(
            "pythonOCC is required for STEP extraction. "
            "Install via: conda install -c conda-forge pythonocc-core"
        )


class StepExtractor:
    """Parses a STEP file and extracts structured geometry as a PartGeometry object."""

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"STEP file not found: {file_path}")
        if self.file_path.suffix.lower() not in {".step", ".stp"}:
            raise ValueError(f"Expected a .step or .stp file, got: {self.file_path.suffix}")

    def extract(self) -> PartGeometry:
        """Parse the STEP file and return a complete PartGeometry."""
        _require_occ()
        logger.info("Loading STEP file: %s", self.file_path)
        shape = self._load_shape()

        logger.info("Extracting global properties...")
        volume, surface_area, com, bb_min, bb_max = self._compute_global_properties(shape)

        logger.info("Extracting faces...")
        faces = self._extract_faces(shape)

        logger.info("Extracting edges...")
        edges = self._extract_edges(shape)

        logger.info("Detecting symmetry...")
        symmetry_planes = self._detect_symmetry(faces, bb_min, bb_max)

        logger.info(
            "Extracted %d faces, %d edges. Volume=%.3f mm³", len(faces), len(edges), volume
        )

        logger.info("Building edge–face adjacency...")
        self._populate_edge_face_adjacency(shape, edges, faces)

        return PartGeometry(
            file_name=self.file_path.name,
            bounding_box_min=bb_min,
            bounding_box_max=bb_max,
            volume=volume,
            surface_area=surface_area,
            center_of_mass=com,
            faces=faces,
            edges=edges,
            symmetry_planes=symmetry_planes,
            detected_features=[],  # filled in by FeatureRecognizer
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_shape(self):
        """Read and transfer the STEP file, returning a TopoDS_Shape."""
        reader = STEPControl_Reader()
        status = reader.ReadFile(str(self.file_path))
        if status != IFSelect_RetDone:
            raise RuntimeError(f"Failed to read STEP file: {self.file_path}")

        reader.TransferRoots()
        shape = reader.Shape()

        if shape.IsNull():
            raise RuntimeError("STEP file produced a null shape — file may be empty or corrupt.")

        return shape

    def _compute_global_properties(self, shape):
        """Compute volume, surface area, center of mass, and bounding box."""
        # Volume and center of mass
        vol_props = GProp_GProps()
        brepgprop_VolumeProperties(shape, vol_props)
        volume = vol_props.Mass()
        com_pt = vol_props.CentreOfMass()
        com = Vector3D(com_pt.X(), com_pt.Y(), com_pt.Z())

        # Surface area
        surf_props = GProp_GProps()
        brepgprop_SurfaceProperties(shape, surf_props)
        surface_area = surf_props.Mass()

        # Bounding box
        bbox = Bnd_Box()
        brepbndlib_Add(shape, bbox)
        xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
        bb_min = Vector3D(xmin, ymin, zmin)
        bb_max = Vector3D(xmax, ymax, zmax)

        return volume, surface_area, com, bb_min, bb_max

    def _extract_faces(self, shape) -> list[Face]:
        """Iterate over all faces in the shape and classify each one."""
        faces: list[Face] = []
        face_id = 0

        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            topo_face = topods.Face(explorer.Current())
            adaptor = BRepAdaptor_Surface(topo_face)
            surf_type = adaptor.GetType()

            face_type = _surface_type_map(surf_type)

            # Compute face area
            props = GProp_GProps()
            brepgprop_SurfaceProperties(topo_face, props)
            area = props.Mass()

            # Extract type-specific parameters
            normal = None
            center = None
            radius = None
            minor_radius = None
            axis_vec = None

            if surf_type == GeomAbs_Plane:
                plane = adaptor.Plane()
                n = plane.Axis().Direction()
                normal = Vector3D(n.X(), n.Y(), n.Z())

            elif surf_type == GeomAbs_Cylinder:
                cyl = adaptor.Cylinder()
                loc = cyl.Location()
                ax = cyl.Axis().Direction()
                center = Vector3D(loc.X(), loc.Y(), loc.Z())
                axis_vec = Vector3D(ax.X(), ax.Y(), ax.Z())
                radius = cyl.Radius()

            elif surf_type == GeomAbs_Sphere:
                sph = adaptor.Sphere()
                loc = sph.Location()
                center = Vector3D(loc.X(), loc.Y(), loc.Z())
                radius = sph.Radius()

            elif surf_type == GeomAbs_Torus:
                tor = adaptor.Torus()
                loc = tor.Location()
                center = Vector3D(loc.X(), loc.Y(), loc.Z())
                radius = tor.MajorRadius()
                minor_radius = tor.MinorRadius()

            elif surf_type == GeomAbs_Cone:
                cone = adaptor.Cone()
                loc = cone.Apex()
                ax = cone.Axis().Direction()
                center = Vector3D(loc.X(), loc.Y(), loc.Z())
                axis_vec = Vector3D(ax.X(), ax.Y(), ax.Z())

            faces.append(
                Face(
                    id=face_id,
                    face_type=face_type,
                    area=area,
                    normal=normal,
                    center=center,
                    radius=radius,
                    minor_radius=minor_radius,
                    axis=axis_vec,
                )
            )
            face_id += 1
            explorer.Next()

        return faces

    def _extract_edges(self, shape) -> list[Edge]:
        """Iterate over all edges in the shape and classify each one."""
        edges: list[Edge] = []
        edge_id = 0

        explorer = TopExp_Explorer(shape, TopAbs_EDGE)
        while explorer.More():
            topo_edge = topods.Edge(explorer.Current())

            # Skip degenerate edges (collapsed edges in topology)
            if BRep_Tool.Degenerated(topo_edge):
                explorer.Next()
                continue

            adaptor = BRepAdaptor_Curve(topo_edge)
            curve_type = adaptor.GetType()
            edge_type = _curve_type_map(curve_type)

            # Length via integrating along the curve parameter range
            length = adaptor.LastParameter() - adaptor.FirstParameter()
            # For non-linear curves this is a parameter range, not arc length;
            # use a simple approximation for display purposes.
            # For lines, param range == length directly.
            if curve_type != GeomAbs_Line:
                try:
                    from OCC.Core.GCPnts import GCPnts_AbscissaPoint
                    length = GCPnts_AbscissaPoint.Length(adaptor)
                except Exception:
                    pass  # fall back to param range

            # Start and end points
            p_start = adaptor.Value(adaptor.FirstParameter())
            p_end = adaptor.Value(adaptor.LastParameter())
            start = Vector3D(p_start.X(), p_start.Y(), p_start.Z())
            end = Vector3D(p_end.X(), p_end.Y(), p_end.Z())

            radius = None
            center = None
            axis_vec = None

            if curve_type == GeomAbs_Circle:
                circ = adaptor.Circle()
                loc = circ.Location()
                ax = circ.Axis().Direction()
                center = Vector3D(loc.X(), loc.Y(), loc.Z())
                axis_vec = Vector3D(ax.X(), ax.Y(), ax.Z())
                radius = circ.Radius()

            edges.append(
                Edge(
                    id=edge_id,
                    edge_type=edge_type,
                    length=length,
                    start_point=start,
                    end_point=end,
                    radius=radius,
                    center=center,
                    axis=axis_vec,
                )
            )
            edge_id += 1
            explorer.Next()

        return edges

    def _populate_edge_face_adjacency(
        self, shape, edges: list[Edge], faces: list[Face]
    ) -> None:
        """
        Populate adjacent_face_ids on every edge AND every face.

        Step 1 – edge→face: builds an IndexedMapOfShape of non-degenerate edges
        (same traversal order as _extract_edges, so index-1 == edge_id), then
        iterates over every face registering its id on each of its edges.

        Step 2 – face→face: derives face adjacency from the edge data built in
        step 1 (two faces are adjacent iff they share at least one edge).
        """
        # ---- Step 1: edge → face ------------------------------------------------
        edge_map = TopTools_IndexedMapOfShape()
        exp = TopExp_Explorer(shape, TopAbs_EDGE)
        while exp.More():
            topo_e = topods.Edge(exp.Current())
            if not BRep_Tool.Degenerated(topo_e):
                edge_map.Add(exp.Current())
            exp.Next()

        face_exp = TopExp_Explorer(shape, TopAbs_FACE)
        face_id = 0
        while face_exp.More():
            e_exp = TopExp_Explorer(face_exp.Current(), TopAbs_EDGE)
            while e_exp.More():
                idx = edge_map.FindIndex(e_exp.Current())  # 1-based; 0 = not found
                if idx > 0:
                    eid = idx - 1
                    if eid < len(edges) and face_id not in edges[eid].adjacent_face_ids:
                        edges[eid].adjacent_face_ids.append(face_id)
                e_exp.Next()
            face_id += 1
            face_exp.Next()

        # ---- Step 2: face → face (via shared edges) ------------------------------
        from collections import defaultdict as _dd
        face_nbrs: dict[int, set[int]] = _dd(set)
        for e in edges:
            for fid1 in e.adjacent_face_ids:
                for fid2 in e.adjacent_face_ids:
                    if fid1 != fid2:
                        face_nbrs[fid1].add(fid2)
        for face in faces:
            face.adjacent_face_ids = list(face_nbrs.get(face.id, set()))

    def _detect_symmetry(
        self, faces: list[Face], bb_min: Vector3D, bb_max: Vector3D
    ) -> list[str]:
        """Check for XY, XZ, YZ mirror symmetry by comparing face centroids."""
        planes_found = []

        cx = (bb_min.x + bb_max.x) / 2
        cy = (bb_min.y + bb_max.y) / 2
        cz = (bb_min.z + bb_max.z) / 2

        planar_faces = [f for f in faces if f.face_type == FaceType.PLANAR and f.normal]
        cyl_faces = [f for f in faces if f.face_type == FaceType.CYLINDRICAL and f.center]

        # For each candidate plane, check if every cylindrical face center
        # has a matching face mirrored across the plane.
        tol = max((bb_max.x - bb_min.x), (bb_max.y - bb_min.y), (bb_max.z - bb_min.z)) * 0.02

        def has_mirror(centers, mirror_fn) -> bool:
            if not centers:
                return False
            for c in centers:
                mc = mirror_fn(c)
                if not any(
                    abs(mc.x - o.x) < tol and abs(mc.y - o.y) < tol and abs(mc.z - o.z) < tol
                    for o in centers
                ):
                    return False
            return True

        centers = [f.center for f in cyl_faces]

        # XZ plane (mirror in Y)
        if has_mirror(centers, lambda c: Vector3D(c.x, 2 * cy - c.y, c.z)):
            planes_found.append("XZ")

        # YZ plane (mirror in X)
        if has_mirror(centers, lambda c: Vector3D(2 * cx - c.x, c.y, c.z)):
            planes_found.append("YZ")

        # XY plane (mirror in Z)
        if has_mirror(centers, lambda c: Vector3D(c.x, c.y, 2 * cz - c.z)):
            planes_found.append("XY")

        return planes_found


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _surface_type_map(occ_type) -> FaceType:
    try:
        return {
            GeomAbs_Plane: FaceType.PLANAR,
            GeomAbs_Cylinder: FaceType.CYLINDRICAL,
            GeomAbs_Cone: FaceType.CONICAL,
            GeomAbs_Sphere: FaceType.SPHERICAL,
            GeomAbs_Torus: FaceType.TOROIDAL,
            GeomAbs_BSplineSurface: FaceType.BSPLINE,
        }[occ_type]
    except KeyError:
        return FaceType.UNKNOWN


def _curve_type_map(occ_type) -> EdgeType:
    try:
        return {
            GeomAbs_Line: EdgeType.LINE,
            GeomAbs_Circle: EdgeType.CIRCLE,
            GeomAbs_Ellipse: EdgeType.ELLIPSE,
            GeomAbs_BSplineCurve: EdgeType.BSPLINE,
        }[occ_type]
    except KeyError:
        return EdgeType.UNKNOWN
