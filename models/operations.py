from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OperationType(Enum):
    NEW_SKETCH = "new_sketch"
    SKETCH_LINE = "sketch_line"
    SKETCH_RECTANGLE = "sketch_rectangle"
    SKETCH_CIRCLE = "sketch_circle"
    SKETCH_ARC = "sketch_arc"
    SKETCH_DIMENSION = "sketch_dimension"
    SKETCH_CONSTRAINT = "sketch_constraint"
    CLOSE_SKETCH = "close_sketch"
    EXTRUDE_BOSS = "extrude_boss"
    EXTRUDE_CUT = "extrude_cut"
    REVOLVE_BOSS = "revolve_boss"
    REVOLVE_CUT = "revolve_cut"
    FILLET = "fillet"
    CHAMFER = "chamfer"
    HOLE_WIZARD = "hole_wizard"
    LINEAR_PATTERN = "linear_pattern"
    CIRCULAR_PATTERN = "circular_pattern"
    MIRROR = "mirror"
    SHELL = "shell"


class SketchPlane(Enum):
    FRONT = "front"
    TOP = "top"
    RIGHT = "right"
    CUSTOM = "custom"   # defined by face reference or offset


@dataclass
class Operation:
    """A single SolidWorks operation in the rebuild sequence."""
    step_number: int
    operation_type: OperationType
    parameters: dict                            # operation-specific params
    description: str                            # human-readable explanation
    references: list[str] = field(default_factory=list)  # refs to prior features


@dataclass
class ReconstructionPlan:
    """The full ordered plan Claude produces."""
    summary: str                                # brief description of the part
    base_plane: SketchPlane                     # which plane to start on
    modeling_strategy: str                      # explanation of approach chosen
    operations: list[Operation]
    notes: list[str] = field(default_factory=list)  # caveats, assumptions, alternatives
