from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FaceType(Enum):
    PLANAR = "planar"
    CYLINDRICAL = "cylindrical"
    CONICAL = "conical"
    SPHERICAL = "spherical"
    TOROIDAL = "toroidal"   # fillets, donuts
    BSPLINE = "bspline"     # freeform surfaces
    UNKNOWN = "unknown"


class EdgeType(Enum):
    LINE = "line"
    CIRCLE = "circle"
    ARC = "arc"
    ELLIPSE = "ellipse"
    BSPLINE = "bspline"
    UNKNOWN = "unknown"


@dataclass
class Vector3D:
    x: float
    y: float
    z: float

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z

    def to_list(self) -> list[float]:
        return [self.x, self.y, self.z]


@dataclass
class Face:
    id: int
    face_type: FaceType
    area: float
    normal: Optional[Vector3D] = None           # for planar faces
    center: Optional[Vector3D] = None           # for cylindrical/spherical/toroidal
    radius: Optional[float] = None              # for cylindrical/spherical/toroidal
    minor_radius: Optional[float] = None        # for toroidal (tube radius)
    axis: Optional[Vector3D] = None             # for cylindrical/conical faces
    adjacent_face_ids: list[int] = field(default_factory=list)


@dataclass
class Edge:
    id: int
    edge_type: EdgeType
    length: float
    start_point: Vector3D
    end_point: Vector3D
    radius: Optional[float] = None              # for arcs/circles
    center: Optional[Vector3D] = None           # for arcs/circles
    axis: Optional[Vector3D] = None             # for circles (normal to plane)
    adjacent_face_ids: list[int] = field(default_factory=list)


@dataclass
class PartGeometry:
    """Complete geometric description of the part, sent to Claude."""
    file_name: str
    bounding_box_min: Vector3D
    bounding_box_max: Vector3D
    volume: float
    surface_area: float
    center_of_mass: Vector3D
    faces: list[Face]
    edges: list[Edge]
    symmetry_planes: list[str] = field(default_factory=list)    # e.g. ["XY", "XZ"]
    detected_features: list[dict] = field(default_factory=list)  # high-level features
