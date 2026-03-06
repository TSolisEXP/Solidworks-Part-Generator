"""
Tests for planner/prompts.py and planner/planner.py.

These tests mock the Anthropic API so they run without a network connection
or API key.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from models.geometry import Edge, EdgeType, Face, FaceType, PartGeometry, Vector3D
from models.operations import OperationType, ReconstructionPlan, SketchPlane
from planner.planner import ClaudePlanner, _strip_code_fences
from planner.prompts import SYSTEM_PROMPT, build_user_prompt


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def simple_geometry():
    """A minimal PartGeometry for a 100×50×30mm block."""
    faces = [
        Face(id=i, face_type=FaceType.PLANAR, area=5000.0, normal=Vector3D(0, 0, 1))
        for i in range(6)
    ]
    edges = [
        Edge(id=i, edge_type=EdgeType.LINE, length=100.0,
             start_point=Vector3D(0, 0, 0), end_point=Vector3D(100, 0, 0))
        for i in range(12)
    ]
    return PartGeometry(
        file_name="block.step",
        bounding_box_min=Vector3D(0, 0, 0),
        bounding_box_max=Vector3D(100, 50, 30),
        volume=150000.0,
        surface_area=25000.0,
        center_of_mass=Vector3D(50, 25, 15),
        faces=faces,
        edges=edges,
        symmetry_planes=["XZ", "YZ"],
        detected_features=[],
    )


_VALID_PLAN_JSON = json.dumps({
    "summary": "Simple rectangular block",
    "base_plane": "front",
    "modeling_strategy": "Single extrusion of the rectangular base profile.",
    "operations": [
        {
            "step_number": 1,
            "operation_type": "new_sketch",
            "parameters": {"plane": "front"},
            "description": "Open sketch on Front Plane",
            "references": [],
        },
        {
            "step_number": 2,
            "operation_type": "sketch_rectangle",
            "parameters": {"center": [0, 0], "width": 100, "height": 50},
            "description": "Draw 100×50 mm rectangle centered on origin",
            "references": [],
        },
        {
            "step_number": 3,
            "operation_type": "close_sketch",
            "parameters": {},
            "description": "Close sketch",
            "references": [],
        },
        {
            "step_number": 4,
            "operation_type": "extrude_boss",
            "parameters": {"depth": 30.0, "direction": "normal"},
            "description": "Extrude 30 mm to form base block",
            "references": ["Sketch1"],
        },
    ],
    "notes": ["Part is symmetric about XZ and YZ planes."],
})


# ------------------------------------------------------------------
# Prompt building tests
# ------------------------------------------------------------------

class TestBuildUserPrompt:
    def test_contains_file_name(self, simple_geometry):
        prompt = build_user_prompt(simple_geometry)
        assert "block.step" in prompt

    def test_contains_volume(self, simple_geometry):
        prompt = build_user_prompt(simple_geometry)
        assert "150000" in prompt

    def test_contains_symmetry(self, simple_geometry):
        prompt = build_user_prompt(simple_geometry)
        assert "XZ" in prompt
        assert "YZ" in prompt

    def test_contains_face_summary(self, simple_geometry):
        prompt = build_user_prompt(simple_geometry)
        assert "Total faces: 6" in prompt

    def test_system_prompt_not_empty(self):
        assert len(SYSTEM_PROMPT) > 100
        assert "SolidWorks" in SYSTEM_PROMPT
        assert "JSON" in SYSTEM_PROMPT


# ------------------------------------------------------------------
# Response parsing tests
# ------------------------------------------------------------------

class TestStripCodeFences:
    def test_plain_json(self):
        text = '{"key": "value"}'
        assert _strip_code_fences(text) == text

    def test_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert _strip_code_fences(text) == '{"key": "value"}'

    def test_plain_fence(self):
        text = '```\n{"key": "value"}\n```'
        assert _strip_code_fences(text) == '{"key": "value"}'


class TestClaudePlannerParseResponse:
    def setup_method(self):
        self.planner = ClaudePlanner.__new__(ClaudePlanner)
        self.planner._model = "test-model"

    def test_parse_valid_json(self):
        plan = self.planner._parse_response(_VALID_PLAN_JSON)
        assert isinstance(plan, ReconstructionPlan)
        assert plan.summary == "Simple rectangular block"
        assert plan.base_plane == SketchPlane.FRONT
        assert len(plan.operations) == 4
        assert plan.operations[0].operation_type == OperationType.NEW_SKETCH
        assert plan.operations[3].operation_type == OperationType.EXTRUDE_BOSS

    def test_parse_json_with_fence(self):
        fenced = f"```json\n{_VALID_PLAN_JSON}\n```"
        plan = self.planner._parse_response(fenced)
        assert len(plan.operations) == 4

    def test_unknown_operation_skipped(self):
        data = json.loads(_VALID_PLAN_JSON)
        data["operations"].append({
            "step_number": 5,
            "operation_type": "nonexistent_op",
            "parameters": {},
            "description": "Should be skipped",
            "references": [],
        })
        plan = self.planner._parse_response(json.dumps(data))
        assert len(plan.operations) == 4  # the unknown one is dropped

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            self.planner._parse_response("this is not json")

    def test_unknown_base_plane_defaults_to_front(self):
        data = json.loads(_VALID_PLAN_JSON)
        data["base_plane"] = "diagonal"
        plan = self.planner._parse_response(json.dumps(data))
        assert plan.base_plane == SketchPlane.FRONT


# ------------------------------------------------------------------
# Integration test with mocked API
# ------------------------------------------------------------------

class TestClaudePlannerIntegration:
    def test_plan_calls_api_and_parses(self, simple_geometry):
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=_VALID_PLAN_JSON)]

        with patch("anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_anthropic_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_message

            planner = ClaudePlanner(api_key="test-key", model="claude-test")
            plan = planner.plan(simple_geometry)

        assert isinstance(plan, ReconstructionPlan)
        assert len(plan.operations) == 4
        mock_client.messages.create.assert_called_once()
