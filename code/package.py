"""Build code.zip at the repository root (forward-slash paths, no dataset, caches or archives)."""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".git"}
SKIP_EXT = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".pyc"}


def main():
    out = ROOT / "code.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("README.md", "requirements.txt"):
            z.write(ROOT / name, name)
        for p in sorted((ROOT / "code").rglob("*")):
            if p.is_dir() or SKIP_DIRS & set(p.parts) or p.suffix.lower() in SKIP_EXT or p.name.startswith(".env"):
                continue
            z.write(p, p.relative_to(ROOT).as_posix())
        # the challenge checks for evaluation/<report>.md at the top of the archive
        for name in ("evaluation_report.md", "usage_report.md"):
            z.write(ROOT / "code" / "evaluation" / name, f"evaluation/{name}")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
