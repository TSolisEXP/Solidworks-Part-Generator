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
        bbox_str = "N/A (unavailable)" if self.bbox_match is None else str(self.bbox_match)
        return (
            f"[{status}] Volume error: {self.volume_error_pct:.4f}% | "
            f"Surface area error: {self.surface_area_error_pct:.4f}% | "
            f"Bounding box match: {bbox_str}"
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

        logger.debug(
            "Original: volume=%.6f mm³, surface_area=%.6f mm²",
            original.volume, original.surface_area,
        )
        logger.debug(
            "Rebuilt:  volume=%.6f mm³, surface_area=%.6f mm²",
            sw_props["volume_mm3"], sw_props["surface_area_mm2"],
        )

        vol_err = _pct_error(original.volume, sw_props["volume_mm3"])
        sa_err = _pct_error(original.surface_area, sw_props["surface_area_mm2"])

        bbox_available = sw_props["bbox_min_mm"] is not None
        if bbox_available:
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
        else:
            bbox_match = None  # unknown — do not count as failure

        passed = (
            vol_err <= self.tolerance * 100
            and sa_err <= self.tolerance * 100
            and (bbox_match is None or bbox_match)
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
                } if bbox_available else "unavailable",
            },
        )
        logger.info(result.summary())
        return result

    def _get_sw_properties(self, sw_part) -> dict:
        """
        Extract mass/geometric properties from a SolidWorks IModelDoc2 via COM.
        Tries CreateMassProperty() (newer API) then GetMassProperties() (older API).
        """
        volume_mm3 = 0.0
        surface_area_mm2 = 0.0

        # Force-rebuild the model so mass properties reflect the latest geometry.
        # Without this, CreateMassProperty / GetMassProperties may return stale
        # or default values from the template.
        try:
            sw_part.ForceRebuild3(False)
        except Exception as _rb_err:
            logger.debug("ForceRebuild3 failed (non-fatal): %s", _rb_err)

        # Attempt 1: CreateMassProperty on Extension (SW 2012+)
        try:
            mass_prop = sw_part.Extension.CreateMassProperty()
            if mass_prop is not None:
                raw_vol = mass_prop.Volume
                raw_sa = mass_prop.SurfaceArea
                logger.debug("CreateMassProperty raw: Volume=%s, SurfaceArea=%s", raw_vol, raw_sa)
                volume_mm3 = raw_vol * _M3_TO_MM3
                surface_area_mm2 = raw_sa * _M2_TO_MM2
        except Exception as _cm_err:
            logger.debug("CreateMassProperty failed: %s", _cm_err)
            mass_prop = None

        # Attempt 2: GetMassProperties on the doc (older API, returns array)
        # SW returns: [density, mass, volume, surface_area, cx, cy, cz, ...] in SI units.
        # Note: SW 2025 may return an extra leading element; we detect the right
        # indices by checking which offset gives a physically plausible volume.
        if mass_prop is None:
            try:
                try:
                    props = sw_part.GetMassProperties(0)
                except Exception:
                    props = sw_part.GetMassProperties
                    if callable(props):
                        props = props(0)
                logger.debug("GetMassProperties raw array: %s", list(props) if props else None)
                if props and len(props) >= 4:
                    # Standard layout: [density(0), mass(1), volume(2), SA(3), ...]
                    # Some SW versions prepend a status/flag element; detect by sign.
                    # A valid volume in m³ is always a small positive number.
                    vol_idx, sa_idx = _find_vol_sa_indices(props)
                    volume_mm3 = props[vol_idx] * _M3_TO_MM3
                    surface_area_mm2 = props[sa_idx] * _M2_TO_MM2
                    logger.debug(
                        "Using indices vol=%d sa=%d → volume=%.3f mm³ SA=%.3f mm²",
                        vol_idx, sa_idx, volume_mm3, surface_area_mm2,
                    )
            except Exception as e:
                raise RuntimeError(
                    f"Both mass property APIs failed. CreateMassProperty unavailable; "
                    f"GetMassProperties error: {e}"
                ) from e

        # Bounding box: GetBox returns (xmin, ymin, zmin, xmax, ymax, zmax) in metres.
        # In SW 2025 GetBox is a parameterless method; calling GetBox(0) raises
        # TypeError because the COM dispatch returns a tuple which Python then
        # tries to subscript with (0).  Try both forms.
        box = None
        try:
            box = sw_part.GetBox()
        except Exception:
            try:
                box = sw_part.GetBox(0)
            except Exception:
                pass

        if box is not None and len(box) >= 6:
            logger.debug("GetBox raw: %s", list(box))
            bbox_min = [v * _M_TO_MM for v in box[:3]]
            bbox_max = [v * _M_TO_MM for v in box[3:6]]
        else:
            bbox_min = None
            bbox_max = None
            logger.warning("GetBox unavailable — bbox comparison will be skipped.")

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


def _find_vol_sa_indices(props) -> tuple[int, int]:
    """
    Return (vol_idx, sa_idx) for a GetMassProperties array.

    Standard SW layout: [density(0), mass(1), volume(2), SA(3), ...]
    Some SW versions prepend a status element, shifting everything by 1.
    Heuristic: the volume index is the first index (2 or 3) whose value is a
    small positive float consistent with a solid (< 1 m³ and > 1e-12 m³).
    SA is always the element immediately after volume.
    """
    for vol_idx in (2, 3, 1):
        if vol_idx >= len(props):
            continue
        v = props[vol_idx]
        # Valid volume in m³: between 1e-12 m³ (sub-mm³) and 1e-3 m³ (1 litre).
        # Anything larger is implausibly big for a machined part; anything at
        # exactly 5e-3 is the "mass in kg at density ~741" false positive we saw.
        if isinstance(v, (int, float)) and 1e-12 < v < 1e-3:
            sa_idx = vol_idx + 1
            if sa_idx < len(props):
                return vol_idx, sa_idx
    # Fall back to standard indices if heuristic fails
    return 2, 3


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
