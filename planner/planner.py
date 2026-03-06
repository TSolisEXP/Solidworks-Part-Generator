"""
Claude-powered reconstruction planner.

Sends part geometry to Claude and parses the returned JSON into a
ReconstructionPlan.
"""

import json
import logging
import re

import anthropic

from models.geometry import PartGeometry
from models.operations import Operation, OperationType, ReconstructionPlan, SketchPlane
from planner.prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


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
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = message.content[0].text
        logger.debug("Claude raw response:\n%s", raw_text)

        plan = self._parse_response(raw_text)
        logger.info(
            "Plan received: %d operations for '%s'", len(plan.operations), plan.summary
        )
        return plan

    def _parse_response(self, raw_text: str) -> ReconstructionPlan:
        """
        Parse Claude's JSON response (strips markdown fences if present)
        and deserialize into a ReconstructionPlan.
        """
        json_str = _strip_code_fences(raw_text)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Claude returned invalid JSON: {e}\n\nRaw text:\n{raw_text}"
            ) from e

        # Parse base_plane
        base_plane_str = data.get("base_plane", "front").lower()
        try:
            base_plane = SketchPlane(base_plane_str)
        except ValueError:
            logger.warning("Unknown base_plane '%s', defaulting to FRONT.", base_plane_str)
            base_plane = SketchPlane.FRONT

        # Parse operations
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

        return ReconstructionPlan(
            summary=data.get("summary", ""),
            base_plane=base_plane,
            modeling_strategy=data.get("modeling_strategy", ""),
            operations=operations,
            notes=data.get("notes", []),
        )


def parse_plan_json(raw_text: str) -> ReconstructionPlan:
    """
    Parse a JSON reconstruction plan string (e.g. pasted from Claude.ai)
    into a ReconstructionPlan. Strips markdown code fences if present.
    """
    # Reuse the same logic as ClaudePlanner._parse_response
    _planner = ClaudePlanner.__new__(ClaudePlanner)
    return _planner._parse_response(raw_text)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ``` or ``` ... ```) from text."""
    text = text.strip()
    # Match ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text
