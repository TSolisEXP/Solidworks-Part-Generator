"""
Loads the SolidWorks type library directly from disk and prints the exact
parameter signatures for FeatureExtrusion*, FeatureCut*, etc.

Run with SolidWorks installed (does NOT need SW to be open):
    python tools/discover_sw_api.py
"""

import glob as glob_mod
import sys

try:
    import pythoncom
    from pythoncom import (
        TKIND_INTERFACE, TKIND_DISPATCH, TKIND_COCLASS,
        FUNC_VIRTUAL, FUNC_PUREVIRTUAL, FUNC_DISPATCH,
    )
except ImportError:
    print("ERROR: pywin32 not installed. Run: pip install pywin32")
    sys.exit(1)

# VT type code → human-readable name
_VT = {
    0: "EMPTY", 2: "I2(short)", 3: "I4(long)", 4: "R4(float)", 5: "R8(double)",
    8: "BSTR(str)", 9: "DISPATCH", 11: "BOOL", 12: "VARIANT", 16: "I1",
    17: "UI1", 18: "UI2", 19: "UI4", 20: "I8", 21: "UI8", 22: "INT",
    23: "UINT", 24: "VOID", 25: "HRESULT", 26: "PTR", 27: "SAFEARRAY",
    28: "CARRAY", 29: "USERDEFINED", 30: "LPSTR", 31: "LPWSTR",
}

KEYWORDS = [
    "FeatureExtrusion", "FeatureCut", "FeatureRevolve",
    "HoleWizard", "InsertSketch", "FeatureLinearPattern",
    "FeatureCircularPattern", "FeatureFillet",
]


def vt_str(vt):
    byref = " &" if vt & 0x4000 else ""
    base = vt & ~0x4000
    return _VT.get(base, f"VT_{base:#04x}") + byref


def dump_methods(typeinfo, typeattr, indent="  "):
    """Print all methods (or just keyword-matching ones) from a typeinfo."""
    matched = []
    all_names = []

    for j in range(typeattr.cFuncs):
        try:
            fd = typeinfo.GetFuncDesc(j)
        except Exception:
            continue

        # Try GetDocumentation first (works for dispinterfaces), then GetNames
        try:
            method_name = typeinfo.GetDocumentation(fd.memid)[0]
        except Exception:
            method_name = f"func_{j}"

        # Number of parameters: len(rgelemdescParam) is more reliable than cParams
        try:
            n_params = len(fd.rgelemdescParam)
        except Exception:
            n_params = 0

        # GetNames gives parameter names (need n_params+1 for method name + params)
        try:
            names = typeinfo.GetNames(fd.memid, n_params + 2)
        except Exception:
            names = [method_name]

        all_names.append(method_name)

        if not any(kw.lower() in method_name.lower() for kw in KEYWORDS):
            continue

        param_names = list(names[1:]) if len(names) > 1 else []
        params = []
        for k in range(n_params):
            pname = param_names[k] if k < len(param_names) else f"p{k+1}"
            try:
                vt = fd.rgelemdescParam[k].tdesc.vt
                params.append(f"{vt_str(vt)} {pname}")
            except Exception:
                params.append(pname)

        matched.append((method_name, n_params, params))

    print(f"{indent}  total funcs: {typeattr.cFuncs}, keyword matches: {len(matched)}")
    if matched:
        for method_name, nparam, params in matched:
            print(f"\n{indent}  >>> {method_name}({nparam} params):")
            for idx, p in enumerate(params, 1):
                print(f"{indent}      {idx:2d}. {p}")
    else:
        # Show first 20 method names so we can see what's actually there
        if all_names:
            print(f"{indent}  (no keyword matches; first methods: {all_names[:20]})")
        else:
            print(f"{indent}  (no funcs found in this typeinfo)")


def inspect_typelib(typelib):
    n = typelib.GetTypeInfoCount()
    print(f"Total type infos: {n}")

    # Collect all typeinfos and their kinds
    infos = {}  # name -> (typeinfo, typeattr, tkind)
    for i in range(n):
        try:
            name = typelib.GetDocumentation(i)[0]
            typeinfo = typelib.GetTypeInfo(i)
            typeattr = typeinfo.GetTypeAttr()
            infos[name] = (typeinfo, typeattr, typeattr.typekind)
        except Exception:
            continue

    for name, (typeinfo, typeattr, tkind) in infos.items():
        if "featuremanager" not in name.lower():
            continue

        kind_str = {
            TKIND_INTERFACE: "INTERFACE",
            TKIND_DISPATCH: "DISPINTERFACE",
            TKIND_COCLASS: "COCLASS",
        }.get(tkind, f"kind={tkind}")

        print(f"\n[{kind_str}: {name}]  (cFuncs={typeattr.cFuncs}, cImplTypes={typeattr.cImplTypes})")
        dump_methods(typeinfo, typeattr)


def main():
    import re

    candidates = glob_mod.glob(
        r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS*\sldworks.tlb"
    )
    candidates += glob_mod.glob(
        r"C:\Program Files\SolidWorks*\sldworks.tlb"
    )
    candidates = list(set(candidates))

    def _sw_sort_key(p):
        m = re.search(r'SOLIDWORKS\s*\((\d+)\)', p, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    tlb_files = sorted(candidates, key=_sw_sort_key, reverse=True)

    if not tlb_files:
        print("No sldworks.tlb found.")
        sys.exit(1)

    target = tlb_files[0]
    print(f"Loading: {target}\n")

    try:
        typelib = pythoncom.LoadTypeLib(target)
    except Exception as e:
        print(f"ERROR loading type lib: {e}")
        sys.exit(1)

    inspect_typelib(typelib)
    print("\nDone.")


if __name__ == "__main__":
    main()
