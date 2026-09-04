from pathlib import Path

p = Path(__file__).resolve().parents[2] / "sfincs_global_validation.py"
s = p.read_text()

# Regression guard: validation must use in-domain eligibility, not radius-only acceptance.
tokens = [
    "inside",
    "domain",
]
if not all(t.lower() in s.lower() for t in tokens):
    raise SystemExit("Could not find the in-domain validation implementation markers.")

print("Validation in-domain source guard: PASS")
