#!/usr/bin/env python3

from datetime import datetime, timedelta
from json import loads
from os import getenv, sep
from os.path import join
from pathlib import Path
from sys import exit as sys_exit, path as sys_path
from traceback import format_exc

for deps_path in [join(sep, "usr", "share", "bunkerweb", *paths) for paths in (("deps", "python"), ("utils",), ("db",), ("core", "backup"))]:
    if deps_path not in sys_path:
        sys_path.append(deps_path)

from logger import setup_logger  # type: ignore
from jobs import Job  # type: ignore
from utils import backup_database, update_cache_file

LOGGER = setup_logger("BACKUP", getenv("LOG_LEVEL", "INFO"))
status = 0

try:
    backup_dir = Path(getenv("BACKUP_DIRECTORY", "/var/lib/bunkerweb/backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Check if backup is activated
    if getenv("USE_BACKUP", "yes") == "no":
        LOGGER.info("Backup feature is disabled, skipping backup ...")
        sys_exit(0)

    JOB = Job(LOGGER)

    last_backup = loads(JOB.get_cache("backup.json") or "{}")
    last_backup_date = last_backup.get("date", None)
    if last_backup_date:
        last_backup_date = datetime.fromisoformat(last_backup_date).astimezone()

    current_time = datetime.now().astimezone()
    backup_period = getenv("BACKUP_SCHEDULE", "daily")
    PERIOD_STAMPS = {
        "daily": timedelta(days=1).total_seconds(),
        "weekly": timedelta(weeks=1).total_seconds(),
        "monthly": timedelta(weeks=4).total_seconds(),
    }

    already_done = last_backup_date and last_backup_date.timestamp() + PERIOD_STAMPS[backup_period] > current_time.timestamp()
    backup_rotation = int(getenv("BACKUP_ROTATION", "7"))

    sorted_files = []
    if already_done:

        # Get all backup files in the directory
        backup_files = backup_dir.glob("backup-*.zip")

        # Sort the backup files by name
        sorted_files = sorted(backup_files)

    if len(sorted_files) <= backup_rotation and already_done:
        LOGGER.info(f"Backup already done within the last {backup_period} period, skipping backup ...")
        sys_exit(0)

    if not already_done:
        db_metadata = JOB.db.get_metadata()

        if isinstance(db_metadata, str) or db_metadata["scheduler_first_start"]:
            LOGGER.info("First start of the scheduler, skipping backup ...")
            sys_exit(0)

        backup_database(current_time, JOB.db, backup_dir)

        # Get all backup files in the directory
        backup_files = backup_dir.glob("backup-*.zip")

        # Sort the backup files by name
        sorted_files = sorted(backup_files)

    # Check if the number of backup files exceeds the rotation limit
    if len(sorted_files) > backup_rotation:
        # Calculate the number of files to remove
        num_files_to_remove = len(sorted_files) - backup_rotation

        # Remove the oldest backup files
        for file in sorted_files[:num_files_to_remove]:
            LOGGER.warning(f"Removing old backup file: {file}, as the rotation limit has been reached ...")
            file.unlink()

    update_cache_file(JOB.db, backup_dir)
except SystemExit as e:
    status = e.code
except:
    status = 2
    LOGGER.error(f"Exception while running backup-data.py :\n{format_exc()}")

sys_exit(status)
