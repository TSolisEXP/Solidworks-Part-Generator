import os

# Anthropic API
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

# Ollama local LLM (free, no API key required)
OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b")

# Logging level
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
