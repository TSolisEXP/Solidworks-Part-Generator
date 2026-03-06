"""
Utility for renaming SolidWorks features in the feature tree.
"""

import logging

logger = logging.getLogger(__name__)


def rename_feature(feature, name: str) -> bool:
    """
    Rename a SolidWorks IFeature to the given name.

    Returns True on success, False if the feature or rename failed silently.
    """
    if feature is None:
        logger.warning("rename_feature called with None feature (name='%s').", name)
        return False
    try:
        feature.Name = name
        return True
    except Exception as e:
        logger.warning("Failed to rename feature to '%s': %s", name, e)
        return False
