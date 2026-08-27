#!/usr/bin/env python3
"""Read-only forensic audit for silhouette number ambiguity.

This script scans the historical silhouette archive, compares each label with
the source shot annotations, and writes review artifacts under
``outputs/tests/silhouette-number-audit/``. It never mutates canonical project
state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.silhouette_number_audit import audit_silhouette_number_provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the read-only silhouette number-ambiguity audit.",
    )
    parser.add_argument(
        "--project",
        default=".",
        help="Path to the project root (default: current directory).",
    )
    parser.add_argument(
        "--media-type",
        default="movie",
        choices=("movie", "gameplay"),
        help="Media type to audit (default: movie).",
    )
    parser.add_argument(
        "--audit-dir",
        default=None,
        help="Override the output directory (default: outputs/tests/silhouette-number-audit).",
    )
    args = parser.parse_args(argv)

    report = audit_silhouette_number_provenance(
        Path(args.project).resolve(),
        media_type=args.media_type,
        audit_dir=Path(args.audit_dir).resolve() if args.audit_dir else None,
    )

    print(f"Saved: {report['report_md']}")
    print(f"Saved: {report['report_json']}")
    print(f"Saved: {report['summary_csv']}")
    print(f"Saved: {report['provenance_csv']}")
    print(
        "Classification: "
        f"valid={report['classification']['valid']} "
        f"number={report['classification']['questionable_number']} "
        f"split={report['classification']['questionable_split']} "
        f"partial={report['classification']['questionable_partial']} "
        f"unsupported={report['classification']['questionable_unsupported']} "
        f"unverifiable={report['classification']['unverifiable']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
