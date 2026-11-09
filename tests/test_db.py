import sqlite3

from sqlmodel import select

from ai_factory.config import get_settings
from ai_factory.core.models import Project, ProjectPhase
from ai_factory.db import reset_engine_cache, session


def test_old_database_gets_new_columns():
    """A v1 database (no M2 columns) is upgraded in place on startup."""
    path = get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE project (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
        "repo_url VARCHAR NOT NULL)"
    )
    con.execute("INSERT INTO project (name, repo_url) VALUES ('old', 'x')")
    con.commit()
    con.close()
    reset_engine_cache()

    with session() as s:
        p = s.exec(select(Project)).one()
    assert p.phase == ProjectPhase.SPECIFY
    assert p.role_bindings == {} and p.max_task_files == 3 and p.paused is False
