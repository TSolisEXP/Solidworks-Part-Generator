import os

# Anthropic API — only required when using --planner api
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# Claude model to use for reconstruction planning
CLAUDE_MODEL: str = "claude-sonnet-4-6"

# SolidWorks part template path (Windows default install)
SOLIDWORKS_TEMPLATE_PATH: str = os.environ.get(
    "SOLIDWORKS_TEMPLATE_PATH",
    r"C:\ProgramData\SolidWorks\templates\Part.prtdot",
)

# Validation: maximum allowable fractional error (0.001 = 0.1%)
VALIDATION_TOLERANCE: float = 0.001

# Logging level
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
