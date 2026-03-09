"""
Minimal direct test of SolidWorks COM API calls.
Does NOT need a STEP file or Claude API key.

Creates a new part, draws a 50x30 rectangle on the Front Plane,
and extrudes it 20 mm.

Run with SolidWorks open (or let it launch):
    python tools/test_com_api.py
"""

import sys
import traceback

try:
    import pythoncom
    import win32com.client as win32
except ImportError:
    print("ERROR: pywin32 not installed. Run: pip install pywin32")
    sys.exit(1)

_NULL_DISPATCH = win32.VARIANT(pythoncom.VT_DISPATCH, None)

# SolidWorks API constants
swEndCondBlind = 0
swEndCondThroughAll = 1
_MM = 1e-3


def step(label, fn):
    """Run fn(), print OK or FAIL with traceback."""
    print(f"  {label} ... ", end="", flush=True)
    try:
        result = fn()
        print(f"OK  ->  {result!r}")
        return result
    except Exception:
        print("FAIL")
        traceback.print_exc()
        return None


def main():
    print("=== SolidWorks COM API test ===\n")

    # 1. Connect
    print("[1] Connecting to SolidWorks...")
    try:
        app = win32.GetActiveObject("SldWorks.Application")
        print("    Attached to existing SW instance.")
    except Exception:
        print("    No running SW instance — launching...")
        app = win32.Dispatch("SldWorks.Application")
        app.Visible = True

    # 2. Resolve template
    print("\n[2] Finding part template...")
    import glob, os
    candidates = sorted(
        glob.glob(r"C:\ProgramData\SolidWorks\**\Part.prtdot", recursive=True),
        reverse=True,
    )
    if not candidates:
        print("    ERROR: No Part.prtdot found under C:\\ProgramData\\SolidWorks")
        sys.exit(1)
    template = candidates[0]
    print(f"    Template: {template}")

    # 3. New part
    print("\n[3] Creating new part document...")
    app.NewDocument(template, 0, 0, 0)
    part = app.ActiveDoc
    if part is None:
        print("    ERROR: ActiveDoc is None after NewDocument")
        sys.exit(1)
    fmgr = part.FeatureManager
    smgr = part.SketchManager
    print("    Part created OK")

    # 4. Select Front Plane
    print("\n[4] Selecting Front Plane...")
    part.ClearSelection2(True)
    ok = step(
        "SelectByID2(Front Plane, PLANE, ..., _NULL_DISPATCH, ...)",
        lambda: part.Extension.SelectByID2(
            "Front Plane", "PLANE", 0.0, 0.0, 0.0, False, 0, _NULL_DISPATCH, 0
        ),
    )
    if not ok:
        print("    !! SelectByID2 failed — plane not selected")

    # 5. Insert sketch
    print("\n[5] Opening sketch...")
    step("InsertSketch(True)", lambda: smgr.InsertSketch(True))

    # 6. Draw a 50x30 rectangle (corner-based)
    print("\n[6] Drawing 50x30 mm rectangle...")
    rect = step(
        "CreateCornerRectangle(0,0,0, 50mm,30mm,0)",
        lambda: smgr.CreateCornerRectangle(
            0.0, 0.0, 0.0,
            50 * _MM, 30 * _MM, 0.0,
        ),
    )

    # 7. Close sketch
    print("\n[7] Closing sketch...")
    step("InsertSketch(True) [close]", lambda: smgr.InsertSketch(True))

    depth = 20 * _MM

    def _make_sketch():
        """Open a fresh sketch with a 50x30 mm rectangle and close it."""
        part.ClearSelection2(True)
        part.Extension.SelectByID2("Front Plane", "PLANE", 0.0, 0.0, 0.0, False, 0, _NULL_DISPATCH, 0)
        smgr.InsertSketch(True)
        smgr.CreateCornerRectangle(0.0, 0.0, 0.0, 50 * _MM, 30 * _MM, 0.0)
        smgr.InsertSketch(True)   # close

    # We already have a closed sketch from step 7.
    # Each attempt below will re-open a sketch if the previous one was consumed.

    feature = None
    attempts = [
        # (method_name, param_list)
        ("FeatureExtrusion2", [
            True, False, False,
            swEndCondBlind, swEndCondBlind,
            depth, 0.0,
            False, False, True, True, 0.0, 0.0,
            False, False, False, False,
            True, False, True,
            # 21-23: start condition params (added in later SW versions)
            0,    # T0  — swStartSketchPlane = 0
            0.0,  # StartOffset
            False,# FlipStartOffset
        ]),
        ("FeatureExtrusion3", [
            True, False, False,
            swEndCondBlind, swEndCondBlind,
            depth, 0.0,
            False, False, True, True, 0.0, 0.0,
            False, False, False, False,
            True, False, True,
            0, 0.0, False,  # T0, StartOffset, FlipStartOffset
        ]),
    ]

    for method_name, params in attempts:
        if feature is not None:
            break
        print(f"\n[8] Extruding 20 mm with {method_name} ({len(params)} params)...")
        feature = step(
            f"{method_name}({len(params)} params)",
            lambda m=method_name, p=params: getattr(fmgr, m)(*p),
        )
        if feature is None:
            print(f"    Will retry with next candidate...")
            _make_sketch()

    # 9. Test FeatureCut — draw a small circle on Top Plane and cut through
    if feature is not None:
        print("\n[9] Testing cut methods (through-all)...")

        # Base 16 params for FeatureCut3, extended with extras up to 40
        # Pad with alternating False/0/0.0 to probe param count
        _base_cut = [
            True, False, True,              # 1-3:  Sd, Flip, Dir
            swEndCondThroughAll, swEndCondThroughAll,  # 4-5: T1, T2
            0.0, 0.0,                       # 6-7:  D1, D2
            False, False, True, True,       # 8-11: Dchk1, Dchk2, Ddir1, Ddir2
            0.0, 0.0,                       # 12-13: Dang1, Dang2
            False,                          # 14: NormalCut
            False,                          # 15: UseFeatScope
            True,                           # 16: UseAutoSelect
            0,                              # 17: T0 = swStartSketchPlane
            0.0,                            # 18: StartOffset
            False,                          # 19: FlipStartOffset
            # padding to probe beyond 19 — mix of bool/int/double
            False, False, False, False,     # 20-23
            False, False, False, False,     # 24-27
            False, False, False, False,     # 28-31
            False, False, False, False,     # 32-35
            False, False, False, False,     # 36-39
            False,                          # 40
        ]

        def _draw_cut_sketch():
            part.ClearSelection2(True)
            part.Extension.SelectByID2("Top Plane", "PLANE", 0.0, 0.0, 0.0, False, 0, _NULL_DISPATCH, 0)
            smgr.InsertSketch(True)
            smgr.CreateCircle(0.0, 0.0, 0.0, 5 * _MM, 0.0, 0.0)
            smgr.InsertSketch(True)

        _draw_cut_sketch()

        DISP_E_BADPARAMCOUNT = -2147352562   # "Invalid number of parameters" — wrong count
        DISP_E_PARAMNOTOPTIONAL = -2147352561  # "Parameter not optional" — too few

        cut_feature = None
        for method in ("FeatureCut3", "FeatureCut4"):
            if cut_feature is not None:
                break
            for n in range(16, 41):
                p = _base_cut[:n]
                try:
                    cut_feature = getattr(fmgr, method)(*p)
                    print(f"  {method}({n} params) -> SUCCESS!  feature={cut_feature!r}")
                    break
                except Exception as e:
                    code = getattr(e, 'hresult', None) or (e.args[0] if e.args else None)
                    tag = {DISP_E_BADPARAMCOUNT: "WRONG_COUNT", DISP_E_PARAMNOTOPTIONAL: "TOO_FEW"}.get(code, f"ERR {code:#010x}")
                    print(f"  {method}({n} params) -> {tag}")
                    if code == DISP_E_BADPARAMCOUNT:
                        # exact count is wrong; stop trying more for this method
                        break
                    # "too few" — try one more
                    _draw_cut_sketch()

        if cut_feature is not None:
            print("  Cut succeeded!")
        else:
            print("  All cut attempts FAILED.")

    # 10. Probe HoleWizard5 param count
    print("\n[10] Probing HoleWizard5 param count...")
    # Select the top face of the extruded block (at Y=20mm, center)
    part.ClearSelection2(True)
    part.Extension.SelectByID2("", "FACE", 0.0, 20 * _MM, 0.0, False, 0, _NULL_DISPATCH, 0)

    _base_hole = [
        0,      # 1. HoleType (0=simple)
        0,      # 2. Standard
        0,      # 3. FastenerType
        swEndCondThroughAll,  # 4. T1EndCondition
        0.0,    # 5. T1Depth
        5 * _MM,# 6. Diameter
        0.0,    # 7. DrillAngle
        0.0,    # 8. CounterboreDiameter
        0.0,    # 9. CounterboreDepth
        0.0,    # 10. CountersinkAngle
        False,  # 11. ReverseDir
        False,  # 12. FlipSideToMaterial
        False,  # 13. (was string ConfigName — trying False)
        False,  # 14. AddDim
        # padding to probe beyond documented 14
        False, False, False, False,
        False, False, False, False,
        False, False, False, False,
        False, False, False, False,
        False, False, False, False,
        False, False, False, False,
    ]

    DISP_E_BADPARAMCOUNT = -2147352562
    DISP_E_PARAMNOTOPTIONAL = -2147352561

    hole_feature = None
    for n in range(14, 40):
        p = _base_hole[:n]
        # Re-select face before each attempt
        part.ClearSelection2(True)
        part.Extension.SelectByID2("", "FACE", 0.0, 20 * _MM, 0.0, False, 0, _NULL_DISPATCH, 0)
        try:
            hole_feature = fmgr.HoleWizard5(*p)
            print(f"  HoleWizard5({n} params) -> SUCCESS!  feature={hole_feature!r}")
            break
        except Exception as e:
            code = getattr(e, 'hresult', None) or (e.args[0] if e.args else None)
            tag = {DISP_E_BADPARAMCOUNT: "WRONG_COUNT", DISP_E_PARAMNOTOPTIONAL: "TOO_FEW"}.get(code, f"ERR {code:#010x}")
            print(f"  HoleWizard5({n} params) -> {tag}")
            if code == DISP_E_BADPARAMCOUNT:
                break

    if hole_feature is not None:
        # Close the position sketch SW auto-opened
        smgr.InsertSketch(True)

    print("\n=== Summary ===")
    if feature is not None:
        print("SUCCESS - extrude feature created!")
        print("The part is open in SolidWorks. Close without saving or save as you like.")
    else:
        print("FAIL - feature is None (see errors above).")
        print("Check SolidWorks for any error dialogs.")


if __name__ == "__main__":
    main()
