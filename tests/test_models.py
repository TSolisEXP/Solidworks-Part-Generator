"""
Unit tests for models/geometry.py and models/operations.py.
No external dependencies required.
"""

import pytest

from models.geometry import (
    Edge,
    EdgeType,
    Face,
    FaceType,
    PartGeometry,
    Vector3D,
)
from models.operations import (
    Operation,
    OperationType,
    ReconstructionPlan,
    SketchPlane,
)


class TestVector3D:
    def test_to_list(self):
        v = Vector3D(1.0, 2.0, 3.0)
        assert v.to_list() == [1.0, 2.0, 3.0]

    def test_iter(self):
        v = Vector3D(4.0, 5.0, 6.0)
        x, y, z = v
        assert (x, y, z) == (4.0, 5.0, 6.0)


class TestFace:
    def test_defaults(self):
        f = Face(id=0, face_type=FaceType.PLANAR, area=100.0)
        assert f.normal is None
        assert f.adjacent_face_ids == []

    def test_cylindrical(self):
        f = Face(
            id=1,
            face_type=FaceType.CYLINDRICAL,
            area=50.0,
            center=Vector3D(0, 0, 0),
            radius=5.0,
            axis=Vector3D(0, 0, 1),
        )
        assert f.radius == 5.0
        assert f.face_type == FaceType.CYLINDRICAL


class TestEdge:
    def test_line_edge(self):
        e = Edge(
            id=0,
            edge_type=EdgeType.LINE,
            length=10.0,
            start_point=Vector3D(0, 0, 0),
            end_point=Vector3D(10, 0, 0),
        )
        assert e.radius is None
        assert e.length == 10.0

    def test_circle_edge(self):
        e = Edge(
            id=1,
            edge_type=EdgeType.CIRCLE,
            length=31.415,
            start_point=Vector3D(5, 0, 0),
            end_point=Vector3D(5, 0, 0),
            radius=5.0,
            center=Vector3D(0, 0, 0),
        )
        assert e.radius == 5.0


class TestPartGeometry:
    def test_construction(self):
        geom = PartGeometry(
            file_name="test.step",
            bounding_box_min=Vector3D(0, 0, 0),
            bounding_box_max=Vector3D(100, 50, 30),
            volume=150000.0,
            surface_area=25000.0,
            center_of_mass=Vector3D(50, 25, 15),
            faces=[],
            edges=[],
        )
        assert geom.file_name == "test.step"
        assert geom.symmetry_planes == []
        assert geom.detected_features == []


class TestOperation:
    def test_construction(self):
        op = Operation(
            step_number=1,
            operation_type=OperationType.EXTRUDE_BOSS,
            parameters={"depth": 20.0, "direction": "normal"},
            description="Base extrude 20mm",
        )
        assert op.references == []
        assert op.operation_type == OperationType.EXTRUDE_BOSS


class TestReconstructionPlan:
    def test_construction(self):
        ops = [
            Operation(
                step_number=1,
                operation_type=OperationType.NEW_SKETCH,
                parameters={"plane": "front"},
                description="Open sketch on front plane",
            ),
            Operation(
                step_number=2,
                operation_type=OperationType.SKETCH_RECTANGLE,
                parameters={"center": [0, 0], "width": 100, "height": 50},
                description="Draw base rectangle",
            ),
            Operation(
                step_number=3,
                operation_type=OperationType.CLOSE_SKETCH,
                parameters={},
                description="Close sketch",
            ),
            Operation(
                step_number=4,
                operation_type=OperationType.EXTRUDE_BOSS,
                parameters={"depth": 30.0, "direction": "normal"},
                description="Extrude 30mm",
            ),
        ]
        plan = ReconstructionPlan(
            summary="Simple rectangular block",
            base_plane=SketchPlane.FRONT,
            modeling_strategy="Single extrusion of the base profile.",
            operations=ops,
        )
        assert len(plan.operations) == 4
        assert plan.base_plane == SketchPlane.FRONT
        assert plan.notes == []


class TestOperationTypeEnum:
    def test_all_values_unique(self):
        values = [op.value for op in OperationType]
        assert len(values) == len(set(values))

    def test_round_trip(self):
        for op_type in OperationType:
            assert OperationType(op_type.value) == op_type


class TestFaceTypeEnum:
    def test_round_trip(self):
        for ft in FaceType:
            assert FaceType(ft.value) == ft
