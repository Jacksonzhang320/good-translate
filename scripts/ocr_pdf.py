"""CLI wrapper for the existing OCR implementation with a strict exit code."""
import argparse
from pathlib import Path
import sys

from convert import run_ocr


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf")
    parser.add_argument("output_pdf")
    parser.add_argument("--langs", default="eng+chi_sim")
    args = parser.parse_args(argv)
    output = Path(args.output_pdf).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        print(f"OCR output already exists: {output}")
        return 0 if output.stat().st_size else 2
    return 0 if run_ocr(str(Path(args.input_pdf).resolve()), str(output), args.langs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
