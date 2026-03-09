"""
SolidWorks COM connection manager.

Requires pywin32 and a licensed SolidWorks installation on Windows.
"""

import glob
import logging
import os

logger = logging.getLogger(__name__)

try:
    import win32com.client
    import pywintypes
    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False
    logger.warning(
        "pywin32 is not installed or not available. "
        "SolidWorks execution will be disabled. "
        "Install via: pip install pywin32"
    )


class SolidWorksConnection:
    """Manages a connection to a running SolidWorks instance."""

    def __init__(self, template_path: str):
        self.template_path = template_path
        self._app = None
        self._part = None
        self._feature_mgr = None
        self._sketch_mgr = None

    @staticmethod
    def is_available() -> bool:
        """Return True if pywin32 is installed and SolidWorks is reachable."""
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32com.client.GetActiveObject("SldWorks.Application")
            return True
        except Exception:
            return False

    def connect(self):
        """
        Attach to a running SolidWorks instance, or launch one if not running.
        Sets self._app.
        """
        if not _WIN32_AVAILABLE:
            raise RuntimeError(
                "pywin32 is not installed. Cannot connect to SolidWorks."
            )

        try:
            self._app = win32com.client.GetActiveObject("SldWorks.Application")
            logger.info("Attached to existing SolidWorks instance.")
        except Exception:
            logger.info("No running SolidWorks instance found. Launching...")
            self._app = win32com.client.Dispatch("SldWorks.Application")
            self._app.Visible = True

        return self._app

    def new_part(self):
        """
        Create a new part document from the configured template.
        Returns (part, feature_mgr, sketch_mgr).
        """
        if self._app is None:
            self.connect()

        template = self._resolve_template()
        logger.info("Using part template: %s", template)
        self._app.NewDocument(template, 0, 0, 0)
        self._part = self._app.ActiveDoc

        if self._part is None:
            raise RuntimeError(
                f"Failed to create new part from template: {template}\n"
                "Check that the template path exists and SolidWorks is licensed."
            )

        self._feature_mgr = self._part.FeatureManager
        self._sketch_mgr = self._part.SketchManager

        logger.info("Created new SolidWorks part document.")
        return self._part, self._feature_mgr, self._sketch_mgr

    @property
    def app(self):
        return self._app

    @property
    def part(self):
        return self._part

    @property
    def feature_mgr(self):
        return self._feature_mgr

    @property
    def sketch_mgr(self):
        return self._sketch_mgr

    def _resolve_template(self) -> str:
        """
        Return the part template path to use.
        1. If self.template_path exists on disk, use it as-is.
        2. Otherwise, ask the running SolidWorks app for its default template.
        3. Fall back to a glob search under C:\\ProgramData\\SolidWorks.
        """
        if os.path.isfile(self.template_path):
            return self.template_path

        # Ask SolidWorks for the default part template path (swDefaultTemplatePart = 1)
        try:
            sw_template = self._app.GetUserPreferenceStringValue(1)
            if sw_template and os.path.isfile(sw_template):
                logger.info("Using SolidWorks default template: %s", sw_template)
                return sw_template
        except Exception:
            pass

        # Glob search for any Part.prtdot under the SolidWorks ProgramData folder.
        # Sort descending so the newest installed SW version wins (e.g. 2025 > 2019).
        candidates = sorted(
            glob.glob(r"C:\ProgramData\SolidWorks\**\Part.prtdot", recursive=True),
            reverse=True,
        )
        if candidates:
            found = candidates[0]
            logger.info("Auto-detected template: %s", found)
            return found

        raise RuntimeError(
            f"SolidWorks part template not found at '{self.template_path}' and "
            "could not be auto-detected. Set the SOLIDWORKS_TEMPLATE_PATH "
            r"environment variable to the full path of your Part.prtdot file "
            r"(e.g. C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates\Part.prtdot)."
        )

    def save(self, file_path: str):
        """Save the active part to the given path."""
        if self._part is None:
            raise RuntimeError("No active part document to save.")
        errors = win32com.client.VARIANT(pywintypes.VT_BYREF | pywintypes.VT_I4, 0)
        warnings = win32com.client.VARIANT(pywintypes.VT_BYREF | pywintypes.VT_I4, 0)
        self._part.SaveAs3(file_path, 0, 0)
        logger.info("Saved part to: %s", file_path)
