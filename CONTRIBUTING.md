# Contributing to AutoCF

Work from a branch rather than editing `main` directly.

```bash
git checkout -b feature/short-description
# edit and test
python tests/smoke_test.py
python tools/doctor.py --config your_case.yml
git add .
git commit -m "Describe the change"
git push -u origin feature/short-description
```

Open a pull request and merge only after the smoke test and the relevant
AutoCF workflow stage pass.

Do not commit model outputs, downloaded forcing data, API tokens, or HPC
scratch paths for private cases.
