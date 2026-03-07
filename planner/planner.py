"""
Claude-powered reconstruction planner.

Sends part geometry to Claude and parses the returned JSON into a
ReconstructionPlan.  Uses Claude tool-use to enforce the schema.
"""

import json
import logging
import re

import anthropic

from models.geometry import PartGeometry
from models.operations import Operation, OperationType, ReconstructionPlan, SketchPlane
from planner.prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

_VALID_OP_TYPES = [op.value for op in OperationType]

# Tool definition — Claude must call this with our exact schema.
_PLAN_TOOL = {
    "name": "create_reconstruction_plan",
    "description": (
        "Submit the SolidWorks reconstruction plan for the part. "
        "Call this tool with the complete plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Brief description of the part",
            },
            "base_plane": {
                "type": "string",
                "enum": ["front", "top", "right"],
            },
            "modeling_strategy": {
                "type": "string",
                "description": "Explanation of the modeling approach",
            },
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step_number": {"type": "integer"},
                        "operation_type": {
                            "type": "string",
                            "enum": _VALID_OP_TYPES,
                        },
                        "parameters": {"type": "object"},
                        "description": {"type": "string"},
                        "references": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "step_number",
                        "operation_type",
                        "parameters",
                        "description",
                        "references",
                    ],
                },
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "summary",
            "base_plane",
            "modeling_strategy",
            "operations",
            "notes",
        ],
    },
}


class ClaudePlanner:
    """Sends geometry to Claude and returns a ReconstructionPlan."""

    def __init__(self, api_key: str, model: str):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def plan(self, geometry: PartGeometry) -> ReconstructionPlan:
        """Call Claude with the part geometry and return a ReconstructionPlan."""
        user_prompt = build_user_prompt(geometry)

        logger.info("Sending geometry to Claude (%s)...", self._model)
        message = self._client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            tools=[_PLAN_TOOL],
            tool_choice={"type": "tool", "name": "create_reconstruction_plan"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Extract the tool input dict — already validated against the schema.
        tool_block = next(
            (b for b in message.content if b.type == "tool_use"), None
        )
        if tool_block is None:
            raise ValueError(
                "Claude did not call the create_reconstruction_plan tool. "
                f"Response content: {message.content}"
            )

        data = tool_block.input
        logger.debug("Claude tool input:\n%s", json.dumps(data, indent=2))

        plan = _deserialize_plan(data)
        logger.info(
            "Plan received: %d operations for '%s'",
            len(plan.operations),
            plan.summary,
        )
        if not plan.operations:
            logger.warning("Claude returned 0 operations. Tool input:\n%s", json.dumps(data, indent=2))
        return plan


def parse_plan_json(raw_text: str) -> ReconstructionPlan:
    """
    Parse a JSON reconstruction plan string (e.g. pasted from Claude.ai)
    into a ReconstructionPlan. Strips markdown code fences if present.
    """
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        text = match.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Invalid JSON: {e}\n\nRaw text:\n{raw_text}"
        ) from e
    return _deserialize_plan(data)


def _deserialize_plan(data: dict) -> ReconstructionPlan:
    """Convert a plain dict (from tool input or pasted JSON) to a ReconstructionPlan."""
    base_plane_str = data.get("base_plane", "front").lower()
    try:
        base_plane = SketchPlane(base_plane_str)
    except ValueError:
        logger.warning("Unknown base_plane '%s', defaulting to FRONT.", base_plane_str)
        base_plane = SketchPlane.FRONT

    operations: list[Operation] = []
    for op_data in data.get("operations", []):
        op_type_str = op_data.get("operation_type", "")
        try:
            op_type = OperationType(op_type_str)
        except ValueError:
            logger.warning("Unknown operation_type '%s' — skipping.", op_type_str)
            continue

        operations.append(
            Operation(
                step_number=op_data.get("step_number", len(operations) + 1),
                operation_type=op_type,
                parameters=op_data.get("parameters", {}),
                description=op_data.get("description", ""),
                references=op_data.get("references", []),
            )
        )

    if not operations:
        logger.warning(
            "Deserialized 0 operations. Check that the pasted JSON uses the "
            "key 'operations' (not 'steps') and operation_type values from the "
            "valid list. Data received:\n%s",
            json.dumps(data, indent=2),
        )

    return ReconstructionPlan(
        summary=data.get("summary", ""),
        base_plane=base_plane,
        modeling_strategy=data.get("modeling_strategy", ""),
        operations=operations,
        notes=data.get("notes", []),
    )
