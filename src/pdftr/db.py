"""Database access (PostgreSQL + pgvector)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from pdftr.settings import PROJECT_ROOT, get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or get_settings().database_url, row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def is_ready(url: str | None = None) -> bool:
    try:
        with connect(url) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.OperationalError:
        return False


def compose_up(wait_s: float = 90.0) -> None:
    """Start the database container and wait until it accepts connections."""
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"],
        check=True,
        cwd=PROJECT_ROOT,
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if is_ready():
            return
        time.sleep(1.5)
    raise TimeoutError("database did not become ready in time")
