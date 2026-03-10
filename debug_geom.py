"""Quick geometry dump for a STEP file — run in cad-reconstructor conda env."""
import sys, json, math
from extractor.step_extractor import StepExtractor
from extractor.feature_recognition import FeatureRecognizer
from planner.algorithmic_planner import AlgorithmicPlanner

path = sys.argv[1] if len(sys.argv) > 1 else "tests/test_models/testpart2.STEP"

print(f"Extracting: {path}")
ext = StepExtractor()
geom = ext.extract(path)

print(f"\nBounding box: {geom.bounding_box_min} -> {geom.bounding_box_max}")
dims = (
    geom.bounding_box_max.x - geom.bounding_box_min.x,
    geom.bounding_box_max.y - geom.bounding_box_min.y,
    geom.bounding_box_max.z - geom.bounding_box_min.z,
)
print(f"Dims: {dims[0]:.2f} x {dims[1]:.2f} x {dims[2]:.2f} mm")
print(f"Volume: {geom.volume:.2f} mm³")
print(f"Surface area: {geom.surface_area:.2f} mm²")
print(f"Symmetry planes: {geom.symmetry_planes}")

print(f"\nFaces ({len(geom.faces)}):")
from models.geometry import FaceType
face_types = {}
for f in geom.faces:
    face_types[f.face_type.name] = face_types.get(f.face_type.name, 0) + 1
for k, v in sorted(face_types.items()):
    print(f"  {k}: {v}")

print(f"\nEdges ({len(geom.edges)}):")
from models.geometry import EdgeType
edge_types = {}
for e in geom.edges:
    edge_types[e.edge_type.name] = edge_types.get(e.edge_type.name, 0) + 1
for k, v in sorted(edge_types.items()):
    print(f"  {k}: {v}")

print("\nRunning feature recognizer...")
rec = FeatureRecognizer()
geom2 = rec.recognize(geom)
features = geom2.detected_features or []
print(f"Detected {len(features)} features:")
for f in features:
    print(f"  {f}")

print("\nRunning planner...")
plan = AlgorithmicPlanner().plan(geom2)
print(f"Strategy: {plan.modeling_strategy}")
print(f"Operations ({len(plan.operations)}):")
for op in plan.operations:
    print(f"  [{op.step_number}] {op.operation_type.name}: {op.parameters}")
print(f"Notes:")
for n in plan.notes:
    print(f"  - {n}")
