"""
Validation engine: compares the original STEP geometry against the
rebuilt SolidWorks part using volume, bounding box, and surface area.
"""

import logging
import math
from dataclasses import dataclass, field

from models.geometry import PartGeometry

logger = logging.getLogger(__name__)

# Conversion: SolidWorks IMassProperty returns values in metres / m³
_M3_TO_MM3 = 1e9
_M2_TO_MM2 = 1e6
_M_TO_MM = 1e3


@dataclass
class ValidationResult:
    passed: bool
    volume_error_pct: float
    bbox_match: bool
    surface_area_error_pct: float
    details: dict = field(default_factory=dict)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] Volume error: {self.volume_error_pct:.4f}% | "
            f"Surface area error: {self.surface_area_error_pct:.4f}% | "
            f"Bounding box match: {self.bbox_match}"
        )


class ModelValidator:
    """
    Compares a PartGeometry (from pythonOCC extraction) against
    the SolidWorks rebuilt part (via IMassProperty COM API).
    """

    def __init__(self, tolerance: float = 0.001):
        """
        Args:
            tolerance: Maximum allowable fractional error (e.g. 0.001 = 0.1%).
        """
        self.tolerance = tolerance

    def compare(self, original: PartGeometry, sw_part) -> ValidationResult:
        """
        Compare original geometry against the rebuilt SolidWorks part.

        Args:
            original:  PartGeometry extracted from the STEP file.
            sw_part:   IModelDoc2 COM object (the active SolidWorks part document).

        Returns:
            ValidationResult with pass/fail and per-metric errors.
        """
        try:
            sw_props = self._get_sw_properties(sw_part)
        except Exception as e:
            logger.error("Failed to retrieve SolidWorks mass properties: %s", e)
            return ValidationResult(
                passed=False,
                volume_error_pct=float("inf"),
                bbox_match=False,
                surface_area_error_pct=float("inf"),
                details={"error": str(e)},
            )

        vol_err = _pct_error(original.volume, sw_props["volume_mm3"])
        sa_err = _pct_error(original.surface_area, sw_props["surface_area_mm2"])
        bbox_match = _bbox_close(
            original.bounding_box_min,
            original.bounding_box_max,
            sw_props["bbox_min_mm"],
            sw_props["bbox_max_mm"],
            tolerance_mm=max(
                (original.bounding_box_max.x - original.bounding_box_min.x),
                (original.bounding_box_max.y - original.bounding_box_min.y),
                (original.bounding_box_max.z - original.bounding_box_min.z),
            ) * self.tolerance,
        )

        passed = (
            vol_err <= self.tolerance * 100
            and sa_err <= self.tolerance * 100
            and bbox_match
        )

        result = ValidationResult(
            passed=passed,
            volume_error_pct=vol_err,
            bbox_match=bbox_match,
            surface_area_error_pct=sa_err,
            details={
                "original_volume_mm3": original.volume,
                "rebuilt_volume_mm3": sw_props["volume_mm3"],
                "original_surface_area_mm2": original.surface_area,
                "rebuilt_surface_area_mm2": sw_props["surface_area_mm2"],
                "original_bbox": {
                    "min": original.bounding_box_min.to_list(),
                    "max": original.bounding_box_max.to_list(),
                },
                "rebuilt_bbox": {
                    "min": sw_props["bbox_min_mm"],
                    "max": sw_props["bbox_max_mm"],
                },
            },
        )
        logger.info(result.summary())
        return result

    def _get_sw_properties(self, sw_part) -> dict:
        """
        Extract mass/geometric properties from a SolidWorks IModelDoc2 via COM.

        SolidWorks IMassProperty API reference:
            sw_part.Extension.CreateMassProperty() → IMassProperty
            .Volume  (m³)
            .SurfaceArea  (m²)
            .OverrideMassPropData / GetOverrideMassPropData for bounding box
        """
        ext = sw_part.Extension
        mass_prop = ext.CreateMassProperty()

        if mass_prop is None:
            raise RuntimeError("CreateMassProperty returned None — part may have no solid body.")

        volume_mm3 = mass_prop.Volume * _M3_TO_MM3
        surface_area_mm2 = mass_prop.SurfaceArea * _M2_TO_MM2

        # Bounding box: IBody2.GetBodyBox or IModelDoc2.GetBox
        # GetBox returns (xmin, ymin, zmin, xmax, ymax, zmax) in metres
        box = sw_part.GetBox(0)  # 0 = tight bounding box
        if box is not None and len(box) >= 6:
            bbox_min = [v * _M_TO_MM for v in box[:3]]
            bbox_max = [v * _M_TO_MM for v in box[3:6]]
        else:
            bbox_min = [0, 0, 0]
            bbox_max = [0, 0, 0]
            logger.warning("GetBox returned None or unexpected format — bbox comparison will be skipped.")

        return {
            "volume_mm3": volume_mm3,
            "surface_area_mm2": surface_area_mm2,
            "bbox_min_mm": bbox_min,
            "bbox_max_mm": bbox_max,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _pct_error(original: float, rebuilt: float) -> float:
    """Return percentage error, guarded against division by zero."""
    if abs(original) < 1e-10:
        return 0.0 if abs(rebuilt) < 1e-10 else float("inf")
    return abs(original - rebuilt) / abs(original) * 100.0


def _bbox_close(orig_min, orig_max, rebuilt_min: list, rebuilt_max: list, tolerance_mm: float) -> bool:
    """Return True if all six bounding box coordinates are within tolerance."""
    pairs = [
        (orig_min.x, rebuilt_min[0]),
        (orig_min.y, rebuilt_min[1]),
        (orig_min.z, rebuilt_min[2]),
        (orig_max.x, rebuilt_max[0]),
        (orig_max.y, rebuilt_max[1]),
        (orig_max.z, rebuilt_max[2]),
    ]
    return all(abs(a - b) <= tolerance_mm for a, b in pairs)
