import sys, os
os.chdir(r'c:\Users\Tsolis\Documents\GitRepos\Solidworks-Part-Generator')
sys.path.insert(0, r'c:\Users\Tsolis\Documents\GitRepos\Solidworks-Part-Generator')
print('importing StepExtractor...')
from extractor.step_extractor import StepExtractor
from extractor.feature_recognition import FeatureRecognizer
from models.geometry import FaceType, EdgeType

path = r'c:\Users\Tsolis\Documents\GitRepos\Solidworks-Part-Generator\tests\test_models\testpart2.STEP'
print(f'Extracting {path}...')
ext = StepExtractor()
geom = ext.extract(path)
bb_min = geom.bounding_box_min
bb_max = geom.bounding_box_max
print(f'BB: {bb_min.x:.2f},{bb_min.y:.2f},{bb_min.z:.2f} -> {bb_max.x:.2f},{bb_max.y:.2f},{bb_max.z:.2f}')
dims = (bb_max.x-bb_min.x, bb_max.y-bb_min.y, bb_max.z-bb_min.z)
print(f'Dims: {dims[0]:.2f} x {dims[1]:.2f} x {dims[2]:.2f} mm')
face_types = {}
for f in geom.faces:
    face_types[f.face_type.name] = face_types.get(f.face_type.name, 0) + 1
print('Face types:', face_types)
edge_types = {}
for e in geom.edges:
    edge_types[e.edge_type.name] = edge_types.get(e.edge_type.name, 0) + 1
print('Edge types:', edge_types)
print('Symmetry planes:', geom.symmetry_planes)
print()
print('Large faces (area > 100 mm²):')
for f in sorted(geom.faces, key=lambda x: x.area, reverse=True)[:10]:
    n = f.normal
    nstr = f'n=({n.x:.2f},{n.y:.2f},{n.z:.2f})' if n else 'no normal'
    print(f'  {f.face_type.name} area={f.area:.1f} {nstr} id={f.id}')
print()
print('Ellipse/BSPLINE edges:')
for e in geom.edges:
    if e.edge_type.name in ('ELLIPSE', 'BSPLINE', 'OTHER'):
        print(f'  {e.edge_type.name} r={e.radius} center={e.center}')
print()
rec = FeatureRecognizer()
geom2 = rec.recognize(geom)
features = geom2.detected_features or []
print(f'Features ({len(features)}):')
for f in features:
    print(' ', f)
print()
from planner.algorithmic_planner import AlgorithmicPlanner
plan = AlgorithmicPlanner().plan(geom2)
print('Strategy:', plan.modeling_strategy)
print(f'Operations ({len(plan.operations)}):')
for op in plan.operations:
    print(f'  [{op.step_number}] {op.operation_type.name}: {op.parameters}')
