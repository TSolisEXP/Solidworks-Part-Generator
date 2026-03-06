"""
CAD Reconstructor — CLI entry point

Usage:
    python main.py path/to/file.step
    python main.py path/to/file.step --no-execute
    python main.py path/to/file.step --no-execute --output plan.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

import config
from extractor.step_extractor import StepExtractor
from extractor.feature_recognition import FeatureRecognizer
from planner.planner import ClaudePlanner, parse_plan_json
from planner.prompts import build_user_prompt
from executor.sw_connection import SolidWorksConnection
from executor.operations import SolidWorksExecutor
from validator.compare import ModelValidator

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="CAD Reconstructor: STEP → Claude → SolidWorks"
    )
    parser.add_argument("step_file", help="Path to the input STEP file (.step or .stp)")
    parser.add_argument(
        "--no-execute",
        action="store_true",
        help="Extract geometry and generate plan only; skip SolidWorks execution.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help=(
            "Manual mode: print the prompt for Claude.ai, then wait for you to "
            "paste the JSON response. No API key required."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Save the reconstruction plan JSON to this file.",
    )
    parser.add_argument(
        "--log-level",
        default=config.LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    step_path = Path(args.step_file)
    if not step_path.exists():
        console.print(f"[red]Error:[/red] File not found: {step_path}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Phase 1: Extract geometry
    # ------------------------------------------------------------------
    console.print(Panel(f"[bold]CAD Reconstructor[/bold]\n{step_path.name}", expand=False))

    with console.status("Extracting geometry from STEP file..."):
        try:
            extractor = StepExtractor(str(step_path))
            geometry = extractor.extract()
        except Exception as e:
            console.print(f"[red]Extraction failed:[/red] {e}")
            sys.exit(1)

    _print_geometry_summary(geometry)

    # ------------------------------------------------------------------
    # Phase 2: Feature recognition
    # ------------------------------------------------------------------
    with console.status("Running feature recognition..."):
        try:
            recognizer = FeatureRecognizer(geometry.faces, geometry.edges, geometry)
            features = recognizer.recognize()
        except Exception as e:
            console.print(f"[yellow]Feature recognition error (continuing):[/yellow] {e}")
            features = []

    if features:
        console.print(f"[green]Detected {len(features)} high-level features.[/green]")
        for f in features:
            console.print(f"  • {f.get('type', '?')}: {_feature_summary(f)}")
    else:
        console.print("[yellow]No high-level features detected.[/yellow]")

    # ------------------------------------------------------------------
    # Phase 3: Planning (API or manual)
    # ------------------------------------------------------------------
    if args.manual:
        plan = _plan_manual(geometry)
    else:
        if not config.ANTHROPIC_API_KEY:
            console.print(
                "[red]Error:[/red] ANTHROPIC_API_KEY is not set.\n"
                "Either set the environment variable, or use [bold]--manual[/bold] mode "
                "to paste the plan from Claude.ai."
            )
            sys.exit(1)
        with console.status("Sending geometry to Claude API for reconstruction planning..."):
            try:
                planner = ClaudePlanner(api_key=config.ANTHROPIC_API_KEY, model=config.CLAUDE_MODEL)
                plan = planner.plan(geometry)
            except Exception as e:
                console.print(f"[red]Planning failed:[/red] {e}")
                sys.exit(1)

    _print_plan_summary(plan)

    # Optionally save plan JSON
    if args.output:
        _save_plan(plan, args.output)
        console.print(f"[green]Plan saved to:[/green] {args.output}")

    if args.no_execute:
        console.print("[yellow]--no-execute flag set. Skipping SolidWorks execution.[/yellow]")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Phase 4: SolidWorks execution
    # ------------------------------------------------------------------
    if not SolidWorksConnection.is_available():
        console.print(
            "[yellow]SolidWorks is not running or pywin32 is not installed. "
            "Skipping execution. Start SolidWorks and rerun without --no-execute.[/yellow]"
        )
        sys.exit(0)

    with console.status("Connecting to SolidWorks..."):
        conn = SolidWorksConnection(config.SOLIDWORKS_TEMPLATE_PATH)
        conn.connect()
        conn.new_part()

    executor = SolidWorksExecutor(conn)
    with console.status("Executing reconstruction plan in SolidWorks..."):
        results = executor.execute_plan(plan)

    _print_execution_results(results)

    # ------------------------------------------------------------------
    # Phase 5: Validation
    # ------------------------------------------------------------------
    with console.status("Validating rebuilt part..."):
        try:
            validator = ModelValidator(tolerance=config.VALIDATION_TOLERANCE)
            validation = validator.compare(geometry, conn.part)
        except Exception as e:
            console.print(f"[yellow]Validation error:[/yellow] {e}")
            sys.exit(0)

    status_color = "green" if validation.passed else "red"
    console.print(
        Panel(
            f"[{status_color}]{validation.summary()}[/{status_color}]",
            title="Validation Result",
            expand=False,
        )
    )


# ------------------------------------------------------------------
# Manual planning mode
# ------------------------------------------------------------------

def _plan_manual(geometry) -> "ReconstructionPlan":
    """
    Print the Claude prompt, wait for the user to paste the JSON response,
    and parse it into a ReconstructionPlan. No API key required.
    """
    prompt = build_user_prompt(geometry)

    console.print()
    console.rule("[bold yellow]MANUAL MODE — Copy the prompt below into Claude.ai[/bold yellow]")
    console.print()
    console.print(prompt)
    console.rule("[bold yellow]END OF PROMPT[/bold yellow]")
    console.print()
    console.print(
        "[bold]Steps:[/bold]\n"
        "  1. Copy everything between the lines above\n"
        "  2. Paste it into [link=https://claude.ai]claude.ai[/link] or the Claude desktop app\n"
        "  3. Copy Claude's entire JSON response\n"
        "  4. Paste it here, then press [bold]Enter[/bold] twice followed by [bold]Ctrl+Z[/bold] + [bold]Enter[/bold] (Windows) to finish\n"
    )
    console.rule("[bold green]Paste Claude's JSON response below[/bold green]")

    lines = []
    try:
        while True:
            line = input()
            lines.append(line)
    except EOFError:
        pass

    raw_text = "\n".join(lines).strip()

    if not raw_text:
        console.print("[red]No input received. Exiting.[/red]")
        sys.exit(1)

    try:
        plan = parse_plan_json(raw_text)
    except ValueError as e:
        console.print(f"[red]Failed to parse the pasted JSON:[/red] {e}")
        console.print("[yellow]Make sure you copied Claude's full JSON response, not just part of it.[/yellow]")
        sys.exit(1)

    return plan


# ------------------------------------------------------------------
# Display helpers
# ------------------------------------------------------------------

def _print_geometry_summary(geometry):
    table = Table(title="Geometry Summary", show_header=False, box=None)
    table.add_column("Property", style="bold")
    table.add_column("Value")

    bb = geometry.bounding_box_min
    bx = geometry.bounding_box_max
    table.add_row("File", geometry.file_name)
    table.add_row("Faces", str(len(geometry.faces)))
    table.add_row("Edges", str(len(geometry.edges)))
    table.add_row("Volume", f"{geometry.volume:.3f} mm³")
    table.add_row("Surface area", f"{geometry.surface_area:.3f} mm²")
    table.add_row(
        "Bounding box",
        f"({bb.x:.2f}, {bb.y:.2f}, {bb.z:.2f}) → ({bx.x:.2f}, {bx.y:.2f}, {bx.z:.2f}) mm",
    )
    table.add_row(
        "Symmetry planes",
        ", ".join(geometry.symmetry_planes) if geometry.symmetry_planes else "none",
    )
    console.print(table)


def _print_plan_summary(plan):
    console.print(Panel(
        f"[bold]{plan.summary}[/bold]\n\n"
        f"Base plane: {plan.base_plane.value}\n\n"
        f"Strategy: {plan.modeling_strategy}",
        title="Reconstruction Plan",
        expand=False,
    ))

    table = Table(title=f"Operations ({len(plan.operations)})", show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Type", width=20)
    table.add_column("Description")

    for op in plan.operations:
        table.add_row(str(op.step_number), op.operation_type.value, op.description)

    console.print(table)

    if plan.notes:
        console.print("[bold]Notes:[/bold]")
        for note in plan.notes:
            console.print(f"  • {note}")


def _print_execution_results(results: list[dict]):
    table = Table(title="Execution Results", show_header=True)
    table.add_column("Step", width=6)
    table.add_column("Status", width=8)
    table.add_column("Detail")

    for r in results:
        status = "[green]OK[/green]" if r["success"] else "[red]FAIL[/red]"
        table.add_row(str(r["step"]), status, r["detail"])

    console.print(table)


def _feature_summary(f: dict) -> str:
    parts = []
    for k, v in f.items():
        if k in {"type", "id", "face_ids", "_ids", "feature_ids", "note"}:
            continue
        parts.append(f"{k}={v}")
    return ", ".join(parts[:4])  # limit to first 4 fields


def _save_plan(plan, output_path: str):
    data = {
        "summary": plan.summary,
        "base_plane": plan.base_plane.value,
        "modeling_strategy": plan.modeling_strategy,
        "operations": [
            {
                "step_number": op.step_number,
                "operation_type": op.operation_type.value,
                "parameters": op.parameters,
                "description": op.description,
                "references": op.references,
            }
            for op in plan.operations
        ],
        "notes": plan.notes,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    main()
