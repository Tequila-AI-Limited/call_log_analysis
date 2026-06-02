"""Data folder archival utility.

Moves processed weekly raw files from ``data/`` to ``archive/`` so that the
working directory stays clean between pipeline runs.  Files matching the
weekly export patterns (``CallLogLastWeek_*``, ``AbandonedCalls*``, etc.) are
archived; persistent reference files are left in place.

Run manually after verifying the generated report::

    python cleanup_data.py
"""

import glob
import os
import shutil
from datetime import datetime


# Glob patterns for files that should be archived after each pipeline run.
_ARCHIVE_PATTERNS = [
    "CallLogLastWeek_*.csv",
    "CallLogLastWeek_*.xlsx",
    "AbandonedCallslastweek*.csv",
    "AbandonedCallslastweek*.xlsx",
    "InboundCallsLastWeek_*.csv",
    "AgentPerformance_*.csv",
]


def cleanup_data_folder(data_dir: str = "data", archive_dir: str = "archive") -> int:
    """Move processed weekly CSV/XLSX files from ``data_dir`` to ``archive_dir``.

    If a file with the same name already exists in the archive directory it is
    renamed with a timestamp suffix before the new file is moved in, so no
    data is ever silently overwritten.

    Args:
        data_dir: Path to the working data directory.  Defaults to
            ``"data"``.
        archive_dir: Path to the archive directory.  Created automatically if
            it does not exist.  Defaults to ``"archive"``.

    Returns:
        Number of files successfully moved to the archive.
    """
    os.makedirs(archive_dir, exist_ok=True)
    moved_count = 0

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Starting cleanup of '{data_dir}'...")

    for pattern in _ARCHIVE_PATTERNS:
        for src_path in glob.glob(os.path.join(data_dir, pattern)):
            filename = os.path.basename(src_path)
            dest_path = os.path.join(archive_dir, filename)

            try:
                if os.path.exists(dest_path):
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    base, ext = os.path.splitext(filename)
                    timestamped = os.path.join(archive_dir, f"{base}_{timestamp}{ext}")
                    shutil.move(dest_path, timestamped)

                shutil.move(src_path, dest_path)
                print(f"  Archived: {filename}")
                moved_count += 1
            except Exception as exc:
                print(f"  ERROR archiving {filename}: {exc}")

    print(f"Cleanup complete. Moved {moved_count} file(s) to '{archive_dir}'.")
    return moved_count


if __name__ == "__main__":
    cleanup_data_folder()
