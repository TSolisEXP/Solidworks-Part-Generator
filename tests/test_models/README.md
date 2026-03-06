# Test STEP Files

Place sample STEP files here for integration testing. Suggested files:

- `simple_block.step` — a plain rectangular box (e.g., 100×50×30mm)
- `block_with_holes.step` — box with 4 through-holes in a linear pattern
- `bracket.step` — an L-bracket with fillets and chamfers

You can create these in SolidWorks/FreeCAD and export as STEP (AP214 or AP203).

The unit tests in `test_step_extractor.py` will skip automatically if these
files are not present.
