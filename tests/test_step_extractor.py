"""
Tests for extractor/step_extractor.py.

These tests require:
  - pythonocc-core (conda install -c conda-forge pythonocc-core)
  - A sample STEP file at tests/test_models/simple_block.step

Run without STEP files:
  pytest tests/test_step_extractor.py -k "not requires_step"
"""

import pytest
from pathlib import Path

STEP_FILE = Path(__file__).parent / "test_models" / "simple_block.step"


# Skip all file-based tests if pythonocc is not installed
try:
    from extractor.step_extractor import StepExtractor
    _OCC_AVAILABLE = True
except RuntimeError:
    _OCC_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _OCC_AVAILABLE, reason="pythonocc-core not installed")


class TestStepExtractorFileValidation:
    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            StepExtractor("nonexistent.step")

    def test_wrong_extension_raises(self, tmp_path):
        f = tmp_path / "model.obj"
        f.write_text("dummy")
        with pytest.raises(ValueError, match="Expected a .step"):
            StepExtractor(str(f))


@pytest.mark.skipif(not STEP_FILE.exists(), reason="simple_block.step not found in tests/test_models/")
class TestStepExtractorWithFile:
    def test_extract_returns_part_geometry(self):
        from models.geometry import PartGeometry
        extractor = StepExtractor(str(STEP_FILE))
        geom = extractor.extract()
        assert isinstance(geom, PartGeometry)

    def test_has_faces_and_edges(self):
        extractor = StepExtractor(str(STEP_FILE))
        geom = extractor.extract()
        assert len(geom.faces) > 0
        assert len(geom.edges) > 0

    def test_volume_positive(self):
        extractor = StepExtractor(str(STEP_FILE))
        geom = extractor.extract()
        assert geom.volume > 0

    def test_bounding_box_ordered(self):
        extractor = StepExtractor(str(STEP_FILE))
        geom = extractor.extract()
        assert geom.bounding_box_min.x <= geom.bounding_box_max.x
        assert geom.bounding_box_min.y <= geom.bounding_box_max.y
        assert geom.bounding_box_min.z <= geom.bounding_box_max.z
