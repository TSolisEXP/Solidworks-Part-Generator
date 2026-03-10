# CAD Reconstructor — Project Plan

## Purpose

This document is a complete project plan for building a Python application that takes a STEP file (`.step`/`.stp`), analyzes its geometry, and rebuilds it in SolidWorks with a clean, parametric, and easily editable feature tree. Hand this file to Claude in your IDE and use it as the guiding reference for all implementation work.

---

## 1. Project Overview

### What This App Does

1. **Ingests** a STEP (`.step`/`.stp`) CAD file
2. **Extracts** all geometric data — faces, edges, dimensions, topology, feature types
3. **Plans** a reconstruction sequence algorithmically from the extracted geometry (no AI API required)
4. **Executes** the plan through the SolidWorks COM API to produce a new `.sldprt` with a clean feature tree
5. **Validates** the new model against the original by comparing volume, bounding box, and surface deviation

### Why This Matters

CAD models built iteratively often end up with messy, non-parametric feature trees — redundant sketches, suppressed features, boolean hacks. This tool reverse-engineers the *design intent* and produces a model that's easy to modify later.

### Scope Note

This version supports **STEP files only**. Support for SLDPRT, IGES, STL, and other formats is planned for future iterations.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      User Interface                      │
│              (CLI: main.py with rich output)             │
└──────────────┬──────────────────────────┬───────────────┘
               │                          │
               ▼                          ▼
┌──────────────────────┐   ┌──────────────────────────────┐
│  Geometry Extractor  │   │     Validation Engine        │
│  (pythonOCC)         │   │  (compare original vs new)   │
└──────────┬───────────┘   └──────────────▲───────────────┘
           │                              │
           ▼                              │
┌──────────────────────┐   ┌──────────────┴───────────────┐
│  Algorithmic Planner │   │   SolidWorks Executor        │
│  (rule-based, local) │──▶│   (pywin32 COM automation)   │
└──────────────────────┘   └──────────────────────────────┘
         ▲ default
         │ optional fallbacks:
         │  --planner api    (Claude API, requires key)
         │  --planner manual (copy/paste from claude.ai)
```

### Module Breakdown

| Module | File(s) | Responsibility |
|--------|---------|----------------|
| `geometry_extractor` | `extractor/step_extractor.py` | Parse STEP files, extract structured geometry data |
| `feature_recognition` | `extractor/feature_recognition.py` | Detect high-level features from raw geometry |
| `algorithmic_planner` | `planner/algorithmic_planner.py` | Derive reconstruction plan from geometry (no API) |
| `claude_planner` | `planner/planner.py`, `planner/prompts.py` | Optional: send geometry to Claude API, receive plan |
| `sw_executor` | `executor/sw_connection.py`, `executor/operations.py` | Connect to SolidWorks, execute rebuild operations |
| `validator` | `validator/compare.py` | Compare original and rebuilt models |
| `models` | `models/geometry.py`, `models/operations.py` | Shared data classes for geometry and operations |
| `app` | `main.py` | Entry point, orchestration |

---

## 3. Data Models

These are the shared data structures that flow between modules. Define them early — everything depends on them.

### 3.1 Geometry Representation (extractor output → planner input)

```python
# models/geometry.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class FaceType(Enum):
    PLANAR = "planar"
    CYLINDRICAL = "cylindrical"
    CONICAL = "conical"
    SPHERICAL = "spherical"
    TOROIDAL = "toroidal"  # fillets, donuts
    BSPLINE = "bspline"    # freeform surfaces
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

@dataclass
class Face:
    id: int
    face_type: FaceType
    area: float
    normal: Optional[Vector3D]          # for planar faces
    center: Optional[Vector3D]          # for cylindrical/spherical
    radius: Optional[float]             # for cylindrical/spherical/toroidal
    minor_radius: Optional[float]       # for toroidal
    adjacent_face_ids: list[int] = field(default_factory=list)

@dataclass
class Edge:
    id: int
    edge_type: EdgeType
    length: float
    start_point: Vector3D
    end_point: Vector3D
    radius: Optional[float]             # for arcs/circles
    center: Optional[Vector3D]          # for arcs/circles
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
    symmetry_planes: list[str]           # e.g., ["XY", "XZ"]
    detected_features: list[dict]        # high-level recognized features (see 3.3)
```

### 3.2 Operation Representation (planner output → executor input)

```python
# models/operations.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

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
    CUSTOM = "custom"  # defined by face reference or offset

@dataclass
class Operation:
    """A single SolidWorks operation in the rebuild sequence."""
    step_number: int
    operation_type: OperationType
    parameters: dict                    # operation-specific params
    description: str                    # human-readable explanation
    references: list[str] = field(default_factory=list)  # refs to prior features

@dataclass
class ReconstructionPlan:
    """The full ordered plan Claude produces."""
    summary: str                        # brief description of the part
    base_plane: SketchPlane             # which plane to start on
    modeling_strategy: str              # explanation of approach chosen
    operations: list[Operation]
    notes: list[str]                    # caveats, assumptions, alternatives
```

### 3.3 Detected Feature Format

The extractor should identify high-level features before sending to Claude. These help Claude understand the part semantically, not just geometrically.

```python
# Example detected features list
detected_features = [
    {
        "type": "through_hole",
        "diameter": 10.0,
        "center": [30.0, 15.0, 0.0],
        "axis": [0.0, 0.0, 1.0],
        "depth": "through_all"
    },
    {
        "type": "fillet",
        "radius": 3.0,
        "edge_ids": [12, 13, 14, 15]
    },
    {
        "type": "pocket",
        "shape": "rectangular",
        "dimensions": [20.0, 10.0, 5.0],
        "center": [50.0, 25.0, 20.0]
    },
    {
        "type": "boss",
        "shape": "cylindrical",
        "diameter": 15.0,
        "height": 8.0,
        "center": [0.0, 0.0, 25.0]
    },
    {
        "type": "chamfer",
        "distance": 2.0,
        "edge_ids": [22, 23]
    },
    {
        "type": "linear_pattern",
        "feature_ref": "through_hole_1",
        "count": 4,
        "spacing": 20.0,
        "direction": [1.0, 0.0, 0.0]
    },
    {
        "type": "symmetry",
        "plane": "YZ",
        "mirrored_feature_ids": [1, 2, 3]
    }
]
```

---

## 4. Module Implementation Details

### 4.1 Geometry Extractor (STEP Files)

**Library:** `pythonOCC-core` (Python wrapper for OpenCascade)

**Install:** `conda install -c conda-forge pythonocc-core` (conda is strongly recommended over pip for this package)

**What it does:**
- Parse the STEP file into an OpenCascade `TopoDS_Shape`
- Iterate over all faces using `TopExp_Explorer`
- Classify each face by surface type (`BRepAdaptor_Surface`)
- Extract dimensions: plane normals, cylinder radii/axes, fillet radii
- Iterate over all edges, classify and measure
- Compute global properties: volume, surface area, center of mass, bounding box (`GProp_GProps`, `Bnd_Box`)
- Detect symmetry by comparing face groups across candidate mirror planes
- Run feature recognition (see 4.2)

**Key OpenCascade classes you'll use:**
```
STEPControl_Reader          — load STEP files
TopExp_Explorer             — iterate over faces, edges, vertices
BRepAdaptor_Surface         — get surface type and parameters
BRepAdaptor_Curve           — get edge/curve type and parameters
GProp_GProps + BRepGProp    — volume, area, center of mass
Bnd_Box + brepbndlib        — bounding box
BRep_Tool                   — extract geometry from topology
```

**Implementation notes:**
- OpenCascade face types map directly to `GeomAbs_SurfaceType` enum: `GeomAbs_Plane`, `GeomAbs_Cylinder`, `GeomAbs_Cone`, `GeomAbs_Sphere`, `GeomAbs_Torus`, `GeomAbs_BSplineSurface`
- For each cylindrical face, extract radius and axis to detect holes vs bosses
- Group co-axial cylindrical faces to detect counterbores, countersinks
- Planar faces with the same normal that are offset from each other indicate steps/pockets

### 4.2 Feature Recognition Engine

This sub-module sits inside the extractor and does higher-level analysis before sending data to Claude. Claude is good at reasoning about build strategy, but it helps enormously if you pre-digest "these 4 cylindrical faces form 4 through-holes in a linear pattern" rather than making Claude figure that out from raw face data.

**Features to detect (in priority order):**

1. **Through-holes:** cylindrical face pairs with matching radius and coaxial alignment, open on both ends
2. **Blind holes:** cylindrical face with a planar bottom
3. **Fillets:** toroidal or cylindrical faces with small radii connecting two larger faces
4. **Chamfers:** narrow planar faces at 45° (or other angle) between two faces
5. **Pockets/slots:** groups of planar + cylindrical faces forming a depression
6. **Bosses/pads:** groups of faces forming a protrusion
7. **Linear patterns:** repeated identical features at regular spacing along a line
8. **Circular patterns:** repeated identical features at regular angular spacing around an axis
9. **Mirror symmetry:** features that are mirrored across a plane
10. **Shell features:** uniform wall thickness throughout the part

**Detection approach:**
- Compare face groups by type, area, and relative positioning
- Use clustering (e.g., group cylindrical faces by radius, then by axis direction)
- Pattern detection: find repeated features, check if spacing is uniform
- Symmetry: reflect face centroids across candidate planes, check for matches within tolerance

### 4.3 Algorithmic Planner

**File:** `planner/algorithmic_planner.py`

**No API key, no internet, no external dependencies.** Derives the full `ReconstructionPlan` from the `PartGeometry` object using rule-based geometry analysis.

**What it does:**

1. **Classifies the part** as prismatic (extrude-based) or turned (revolve-based) by checking the fraction of cylindrical face area and whether rotational symmetry planes were detected.

2. **Selects the base plane** (front/top/right) using area-weighted voting across all planar face normals — the plane whose axis has the most face area wins.

3. **Extracts the sketch profile** from the actual face boundary edges of the largest planar face perpendicular to the extrude direction. Falls back to a bounding-box rectangle when edge data is unavailable.

4. **Builds revolve profiles** from cylindrical face radii and axial positions, constructing a stepped cross-section.

5. **Emits hole_wizard operations** for each detected hole, computing the entry face point from the bounding box and hole axis direction.

6. **Emits patterns** (linear/circular) referencing the seed hole of each group.

7. **Emits chamfers then fillets** in the correct order (fillets always last).

**Optional: Claude API planner** (`--planner api`)

`planner/planner.py` contains `ClaudePlanner` which sends geometry to the Anthropic API. The `anthropic` package is imported with a try/except guard — it can be omitted from the install if not using API mode. Activated with `--planner api` and requires `ANTHROPIC_API_KEY`.

### 4.4 SolidWorks Executor

**Library:** `pywin32`

**What it does:**
- Connect to a running SolidWorks instance (or launch one)
- Create a new part document
- Execute each `Operation` in the reconstruction plan sequentially
- Name each feature in the tree for clarity

**COM connection pattern:**
```python
import win32com.client

sw_app = win32com.client.Dispatch("SldWorks.Application")
sw_app.Visible = True

# Create new part
model = sw_app.NewDocument(
    "C:\\ProgramData\\SolidWorks\\templates\\Part.prtdot",
    0, 0, 0
)
part = sw_app.ActiveDoc
model_ext = part.Extension
feature_mgr = part.FeatureManager
sketch_mgr = part.SketchManager
```

**Operation implementations you need (build these as individual functions):**

| Operation | Key SW API Methods |
|-----------|-------------------|
| Select plane | `IModelDocExtension.SelectByID2()` |
| New sketch | `ISketchManager.InsertSketch()` |
| Sketch line | `ISketchManager.CreateLine()` |
| Sketch rectangle | `ISketchManager.CreateCornerRectangle()` or `CreateCenterRectangle()` |
| Sketch circle | `ISketchManager.CreateCircle()` |
| Sketch arc | `ISketchManager.CreateArc()` |
| Add dimension | `IModelDoc2.AddDimension2()` or `IDisplayDimension` |
| Extrude boss | `IFeatureManager.FeatureExtrusion3()` |
| Extrude cut | `IFeatureManager.FeatureCut4()` |
| Revolve | `IFeatureManager.FeatureRevolve2()` |
| Fillet | `IFeatureManager.FeatureFillet3()` |
| Chamfer | `IFeatureManager.FeatureChamfer()` |
| Hole wizard | `IFeatureManager.HoleWizard5()` |
| Linear pattern | `IFeatureManager.FeatureLinearPattern4()` |
| Circular pattern | `IFeatureManager.FeatureCircularPattern4()` |
| Mirror | `IFeatureManager.FeatureMirror()` |
| Shell | `IFeatureManager.InsertFeatureShell()` |

**Error handling:** SolidWorks API calls often return `None` or error codes silently. After every feature creation call, check if the feature was actually added by querying the feature tree. If it failed, log the error and either retry with adjusted parameters or halt and report.

**Feature naming:** After creating each feature, rename it to something descriptive using `IFeature.Name`. E.g., "Base Extrude", "Mounting Hole 1", "Edge Fillet R3mm". This is part of making the output "clean."

### 4.5 Validation Engine

**What it does:**
- Compare the rebuilt part to the original
- Report pass/fail with deviation metrics

**Comparison metrics:**
1. **Volume:** compute volume of both parts, compare within tolerance (e.g., 0.1%)
2. **Bounding box:** compare min/max extents
3. **Surface area:** compare total surface area
4. **Face count:** should be identical if rebuild is correct
5. **Point cloud deviation (advanced):** sample points on both surfaces, compute RMS deviation

**For the original STEP file:** use pythonOCC to compute properties (volume, surface area, bounding box) directly from the OpenCascade shape. For the rebuilt part, use the SolidWorks API (`IMassProperty`) to get the same metrics, then compare.

---

## 5. Claude Prompt Engineering (Optional — `--planner api` only)

This section applies only when using the optional Claude API planner (`--planner api`). The default algorithmic planner does not use any of this.

### 5.1 System Prompt

```
You are an expert SolidWorks mechanical designer. Your task is to analyze a
part's geometry and produce an optimal reconstruction plan — a clean, parametric
sequence of SolidWorks features that recreates the part with an editable,
well-organized feature tree.

PRINCIPLES:
- Start with the largest/most defining feature as the base (usually an extrusion
  or revolution)
- Use sketch planes and references that make geometric sense (not arbitrary)
- Prefer symmetric sketches centered on the origin when the part has symmetry
- Use patterns (linear, circular) instead of repeating individual features
- Use mirror features when the part has mirror symmetry
- Add fillets and chamfers LAST (they depend on edges from earlier features)
- Add holes after the main body is established
- Dimension sketches fully — no under-constrained geometry
- Name every feature descriptively

OUTPUT FORMAT:
Return a JSON object matching this schema exactly:
{
  "summary": "Brief description of the part",
  "base_plane": "front" | "top" | "right",
  "modeling_strategy": "Explanation of your approach and why",
  "operations": [
    {
      "step_number": 1,
      "operation_type": "<operation enum value>",
      "parameters": { <operation-specific parameters> },
      "description": "Human-readable description",
      "references": ["names of prior features this depends on"]
    }
  ],
  "notes": ["Any caveats or alternative approaches"]
}

PARAMETER SCHEMAS BY OPERATION TYPE:

new_sketch:
  plane: "front" | "top" | "right" | {"type": "face_ref", "feature": "...", "face_index": 0}
  transform: {"offset": 0.0} (optional, for offset planes)

sketch_rectangle:
  center: [x, y] OR corner1: [x, y], corner2: [x, y]
  Both coordinate values are in mm.

sketch_circle:
  center: [x, y]
  radius: float (mm)

sketch_line:
  start: [x, y]
  end: [x, y]

sketch_arc:
  center: [x, y]
  start: [x, y]
  end: [x, y]

sketch_dimension:
  type: "horizontal" | "vertical" | "diameter" | "radius" | "angle"
  value: float
  entity_refs: [list of sketch entity indices]

close_sketch:
  (no parameters)

extrude_boss:
  depth: float (mm)
  direction: "normal" | "both" | "mid_plane"
  draft_angle: float (degrees, optional, default 0)

extrude_cut:
  depth: float (mm) OR "through_all"
  direction: "normal" | "both"

revolve_boss:
  axis: "x" | "y" | sketch entity reference
  angle: float (degrees, 360 for full revolve)

revolve_cut:
  axis: "x" | "y" | sketch entity reference
  angle: float (degrees)

fillet:
  radius: float (mm)
  edge_selection: description of which edges (e.g., "all edges of Base Extrude top face")

chamfer:
  distance: float (mm)
  angle: float (degrees, default 45)
  edge_selection: description of which edges

hole_wizard:
  type: "simple" | "counterbore" | "countersink" | "tapped"
  diameter: float (mm)
  depth: float (mm) OR "through_all"
  position: [x, y] on sketch plane
  standard: "ANSI Metric" | "ANSI Inch" (optional)

linear_pattern:
  feature_ref: "name of feature to pattern"
  direction: [dx, dy, dz]
  count: int
  spacing: float (mm)
  second_direction: {direction, count, spacing} (optional)

circular_pattern:
  feature_ref: "name of feature to pattern"
  axis: [ax, ay, az] or "feature_axis_ref"
  count: int
  angle: float (degrees, typically 360)

mirror:
  feature_refs: ["names of features to mirror"]
  plane: "front" | "top" | "right" | "custom_plane_ref"

shell:
  thickness: float (mm)
  faces_to_remove: description of open faces
```

### 5.2 User Prompt Template

```
Analyze the following part geometry and produce a reconstruction plan.

## Part Information
- File: {file_name}
- Bounding box: ({bb_min}) to ({bb_max})
- Volume: {volume} mm³
- Surface area: {surface_area} mm²
- Center of mass: ({com})

## Detected Features
{json_features_list}

## Face Summary
- Total faces: {face_count}
- Planar: {planar_count} (normals: {unique_normals})
- Cylindrical: {cyl_count} (radii: {unique_radii})
- Toroidal: {tor_count} (radii: {unique_fillet_radii})
- Other: {other_count}

## Symmetry
{symmetry_info}

## Edge Summary
- Total edges: {edge_count}
- Linear: {linear_count}
- Circular: {circular_count}
- Other: {other_edge_count}

Produce the reconstruction plan as JSON.
```

---

## 6. Project Structure

```
cad-reconstructor/
├── main.py                         # CLI entry point
├── requirements.txt
├── README.md
├── config.py                       # API keys, paths, tolerances
│
├── models/
│   ├── __init__.py
│   ├── geometry.py                 # PartGeometry, Face, Edge, etc.
│   └── operations.py               # Operation, ReconstructionPlan
│
├── extractor/
│   ├── __init__.py
│   ├── step_extractor.py           # STEP → PartGeometry
│   └── feature_recognition.py      # Detect holes, fillets, patterns, etc.
│
├── planner/
│   ├── __init__.py
│   ├── algorithmic_planner.py      # Default: rule-based plan from geometry (no API)
│   ├── planner.py                  # Optional: send geometry to Claude API, get plan
│   └── prompts.py                  # System prompt and user prompt templates (API mode)
│
├── executor/
│   ├── __init__.py
│   ├── sw_connection.py            # Connect/launch SolidWorks
│   ├── operations.py               # Individual operation implementations
│   └── feature_namer.py            # Clean feature naming logic
│
├── validator/
│   ├── __init__.py
│   └── compare.py                  # Original vs rebuilt comparison
│
└── tests/
    ├── test_step_extractor.py
    ├── test_feature_recognition.py
    ├── test_planner.py
    ├── test_executor.py
    └── test_models/                # Sample STEP files for testing
        ├── simple_block.step
        ├── block_with_holes.step
        └── bracket.step
```

---

## 7. Implementation Order

Build the project in this sequence. Each phase produces something testable.

### Phase 1: Foundation (Week 1)
1. Set up project structure, conda environment, install dependencies
2. Implement `models/geometry.py` and `models/operations.py` data classes
3. Implement `extractor/step_extractor.py` — parse a STEP file, extract all faces/edges/properties
4. **Test:** load a simple STEP file (a box with a hole), print the extracted geometry, verify it's correct

### Phase 2: Feature Recognition (Week 2)
5. Implement `extractor/feature_recognition.py` — hole detection, fillet detection, pattern detection
6. Add symmetry detection
7. **Test:** run on increasingly complex STEP files, verify detected features match what you see in a CAD viewer

### Phase 3: Algorithmic Planner (Week 2-3)
8. Implement `planner/algorithmic_planner.py` — classify part, select base plane, extract profile, emit operations
9. **Test:** run `python main.py part.step --no-execute --output plan.json` on test parts, review plans manually
10. Iterate on classification heuristics and profile extraction quality
11. *Optional:* `planner/planner.py` + `planner/prompts.py` already exist for `--planner api` fallback if needed

### Phase 4: SolidWorks Executor (Week 3-4)
11. Implement `executor/sw_connection.py` — connect to SolidWorks
12. Implement `executor/operations.py` — start with sketch + extrude only
13. **Test:** hardcode a simple plan (extrude a rectangle), execute it, verify a part is created
14. Add remaining operations one at a time: cut, fillet, chamfer, hole, pattern, mirror
15. **Test:** execute Claude-generated plans for simple parts

### Phase 5: Validation & End-to-End Testing (Week 4-5)
16. Implement `validator/compare.py` — volume, bounding box, surface area comparison
17. **Test:** full pipeline — STEP in → geometry → Claude → SolidWorks → validate
18. Run against a set of 5-10 test parts of increasing complexity
19. Iterate on Claude prompts and feature recognition based on failures

### Phase 6: Polish (Week 5-6)
20. Build CLI interface with progress output
21. Add error recovery (retry failed operations with adjusted params)
22. Add logging throughout
23. Optional: Streamlit UI for visual review of plans before execution

---

## 8. Dependencies

```
# requirements.txt

# Geometry extraction (install pythonocc-core via conda instead)
# conda install -c conda-forge pythonocc-core
numpy

# SolidWorks COM automation (Windows only)
pywin32

# Utilities
rich            # pretty CLI output

# Optional: only needed for --planner api
# pip install anthropic
```

**Environment notes:**
- This project runs on **Windows only** (SolidWorks is Windows-only)
- Use **conda** for the environment (pythonocc-core doesn't install well via pip)
- SolidWorks must be installed and licensed on the machine
- **No API key is required** for standard use — the algorithmic planner runs entirely locally
- `ANTHROPIC_API_KEY` is only needed if using `--planner api`
- The geometry extractor (pythonOCC) works standalone without SolidWorks, so you can develop and test extraction on any machine — but execution requires SolidWorks

---

## 9. Key Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Feature recognition misses features | Planner has incomplete data, produces bad plan | Start with simple parts; iterate on recognition logic |
| Algorithmic planner misclassifies part type | Wrong base operation (extrude vs revolve) | Review generated plan with `--no-execute --output plan.json` before executing |
| Sketch profile extraction fails | Falls back to bounding-box rectangle | Acceptable for simple parts; improve edge extraction for complex profiles |
| Chamfer size not measured | Placeholder 1.0 mm used | User must verify and adjust chamfer distance after reconstruction |
| SolidWorks API calls fail silently | Missing features in rebuilt part | Check feature tree after every operation; log and report failures |
| Complex freeform surfaces (B-spline) | Can't be recreated with simple features | Detect and warn user; skip freeform parts in MVP |
| STEP files with multiple solid bodies | Out of scope for single-part reconstruction | Detect multi-body STEP files and reject with a clear message |
| STEP files with missing or incomplete data | Extractor can't build full geometry picture | Validate the parsed shape before proceeding; warn user if the STEP file appears incomplete |

---

## 10. Future Enhancements (Post-MVP)

- **SLDPRT support:** read SolidWorks native files via COM API, extract geometry from the evaluated solid body
- **STL support:** mesh → B-Rep conversion using surface fitting algorithms
- **IGES support:** similar to STEP, parseable via pythonOCC
- **Interactive plan editing:** show Claude's plan in a UI, let user modify before execution
- **Learning from feedback:** if user rejects a plan, feed the correction back to improve prompts
- **Multi-body parts:** handle parts with multiple solid bodies
- **Assembly support:** reconstruct assemblies with mates
- **Complexity scoring:** rate the original feature tree vs the new one on editability metrics
- **Batch processing:** process entire folders of parts
- **SolidWorks macro export:** output a VBA macro instead of live execution, for environments where Python COM access isn't available

---

## 11. Getting Started Checklist

- [ ] Install Anaconda/Miniconda
- [ ] Create conda environment: `conda create -n cad-reconstructor python=3.11`
- [ ] Install pythonocc: `conda install -c conda-forge pythonocc-core`
- [ ] Install pip packages: `pip install pywin32 numpy rich`
- [ ] Verify SolidWorks is installed and launchable
- [ ] Find or create 3-5 simple STEP test files (box, cylinder, bracket, plate with holes)
- [ ] Test extraction + planning: `python main.py part.step --no-execute --output plan.json`
- [ ] Review `plan.json`, then run full pipeline with SolidWorks open

**Optional:** `pip install anthropic` and set `ANTHROPIC_API_KEY` only if you want to use `--planner api` for complex parts.

**Tip:** You can develop and test Phases 1-3 (extraction, feature recognition, planning) on any machine — even without SolidWorks. You only need SolidWorks for Phase 4 onward.
