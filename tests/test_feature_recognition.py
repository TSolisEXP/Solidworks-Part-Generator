"""
Tests for extractor/feature_recognition.py.

Uses synthetic Face/Edge data so no STEP file or pythonocc is required.
"""

import math
import pytest

from extractor.feature_recognition import FeatureRecognizer
from models.geometry import Edge, EdgeType, Face, FaceType, PartGeometry, Vector3D


def _make_geometry(faces, edges=None):
    return PartGeometry(
        file_name="synthetic.step",
        bounding_box_min=Vector3D(-50, -50, -50),
        bounding_box_max=Vector3D(50, 50, 50),
        volume=1_000_000.0,
        surface_area=60_000.0,
        center_of_mass=Vector3D(0, 0, 0),
        faces=faces,
        edges=edges or [],
    )


class TestThroughHoleDetection:
    def test_single_through_hole(self):
        """Two cylindrical faces with the same axis/radius → one through_hole."""
        faces = [
            Face(id=0, face_type=FaceType.CYLINDRICAL, area=100.0,
                 center=Vector3D(0, 0, 5), radius=5.0, axis=Vector3D(0, 0, 1)),
            Face(id=1, face_type=FaceType.CYLINDRICAL, area=100.0,
                 center=Vector3D(0, 0, -5), radius=5.0, axis=Vector3D(0, 0, 1)),
            # Large outer face (not a hole)
            Face(id=2, face_type=FaceType.PLANAR, area=10000.0,
                 normal=Vector3D(0, 0, 1)),
        ]
        geom = _make_geometry(faces)
        rec = FeatureRecognizer(faces, [], geom)
        features = rec._detect_through_holes()
        assert any(f["type"] == "through_hole" for f in features)

    def test_no_holes_without_cylinders(self):
        faces = [Face(id=0, face_type=FaceType.PLANAR, area=1000.0, normal=Vector3D(0, 0, 1))]
        geom = _make_geometry(faces)
        rec = FeatureRecognizer(faces, [], geom)
        assert rec._detect_through_holes() == []


class TestFilletDetection:
    def test_toroidal_faces_detected_as_fillets(self):
        faces = [
            Face(id=0, face_type=FaceType.TOROIDAL, area=50.0,
                 center=Vector3D(0, 0, 0), radius=20.0, minor_radius=3.0),
            Face(id=1, face_type=FaceType.TOROIDAL, area=50.0,
                 center=Vector3D(10, 0, 0), radius=20.0, minor_radius=3.0),
        ]
        geom = _make_geometry(faces)
        rec = FeatureRecognizer(faces, [], geom)
        features = rec._detect_fillets()
        assert len(features) == 1
        assert features[0]["type"] == "fillet"
        assert features[0]["radius"] == 3.0
        assert features[0]["face_count"] == 2

    def test_no_toroidal_no_fillets(self):
        faces = [Face(id=0, face_type=FaceType.PLANAR, area=1000.0, normal=Vector3D(0, 0, 1))]
        geom = _make_geometry(faces)
        rec = FeatureRecognizer(faces, [], geom)
        assert rec._detect_fillets() == []


class TestLinearPatternDetection:
    def test_four_holes_in_line(self):
        """Four holes with equal spacing along X → linear_pattern detected."""
        existing = [
            {"type": "through_hole", "id": f"through_hole_{i+1}",
             "diameter": 10.0,
             "center": [float(i * 20), 0.0, 0.0],
             "axis": (0.0, 0.0, 1.0),
             "depth": "through_all",
             "face_ids": [i]}
            for i in range(4)
        ]
        geom = _make_geometry([])
        rec = FeatureRecognizer([], [], geom)
        patterns = rec._detect_linear_patterns(existing)
        assert len(patterns) == 1
        assert patterns[0]["type"] == "linear_pattern"
        assert patterns[0]["count"] == 4

    def test_two_holes_not_a_pattern(self):
        """Only 2 holes — not enough to call a pattern."""
        existing = [
            {"type": "through_hole", "id": "through_hole_1",
             "diameter": 10.0, "center": [0.0, 0.0, 0.0],
             "axis": (0.0, 0.0, 1.0), "depth": "through_all", "face_ids": [0]},
            {"type": "through_hole", "id": "through_hole_2",
             "diameter": 10.0, "center": [20.0, 0.0, 0.0],
             "axis": (0.0, 0.0, 1.0), "depth": "through_all", "face_ids": [1]},
        ]
        geom = _make_geometry([])
        rec = FeatureRecognizer([], [], geom)
        patterns = rec._detect_linear_patterns(existing)
        assert patterns == []


class TestRecognizeIntegration:
    def test_recognize_returns_list(self):
        faces = [
            Face(id=0, face_type=FaceType.PLANAR, area=10000.0, normal=Vector3D(0, 0, 1)),
        ]
        geom = _make_geometry(faces)
        rec = FeatureRecognizer(faces, [], geom)
        result = rec.recognize()
        assert isinstance(result, list)
        # detected_features should be populated on the geometry object
        assert geom.detected_features is result
