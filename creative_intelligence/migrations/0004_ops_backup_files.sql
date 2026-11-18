-- Image files already copied to Drive by the weekly image backup (so each run only sends new ones).
CREATE TABLE IF NOT EXISTS ops_backup_files (
    key           TEXT PRIMARY KEY,           -- '<area>/<file name>'
    size_bytes    INTEGER NOT NULL,
    drive_file_id TEXT NOT NULL,
    at            TEXT NOT NULL
);
