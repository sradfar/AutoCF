from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "sfincs_attribution.py": "917888ccf179855a2edf1e366331cff1bee32c1f7a13c8bbc72b6d5a8c9027f8",
    "sfincs_global_validation.py": "8683b1331d4a7ebe01097a812f0573781ed5d0ea3e72d18e8450e8a08374fa6e",
    "sfincs_hydraulic_core.py": "eed0c8d27fc1f29ae1388bc1febfde19f8960788bff122cacbdf2a58e42aacec",
    "autocf.py": "887aaa68013b7d9ad0049f7a57daf458ebc1f3d6f1c4ef936062a1e05adec24a",
}

bad = []
for rel, expected in EXPECTED.items():
    p = ROOT / rel
    actual = hashlib.sha256(p.read_bytes()).hexdigest()
    print(f"{rel}: {actual}")
    if actual != expected:
        bad.append((rel, expected, actual))

if bad:
    for rel, expected, actual in bad:
        print(f"FAIL {rel}: expected {expected}, got {actual}")
    raise SystemExit(1)

print("Protected AutoCF source hashes: PASS")
