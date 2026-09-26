from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Project

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('TABSIGHT_DATA_DIR', str(ROOT / '.data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'projects').mkdir(exist_ok=True)
(DATA / 'models').mkdir(exist_ok=True)
LOCK = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def connection():
    c = sqlite3.connect(DATA / 'tabsight.sqlite3', timeout=15)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS history (project_id TEXT, revision INTEGER, data TEXT, PRIMARY KEY(project_id,revision))')
    return c


def get_project(pid: str) -> Project:
    with LOCK, connection() as c:
        row = c.execute('SELECT data FROM projects WHERE id=?', (pid,)).fetchone()
    if not row:
        raise KeyError(pid)
    return Project.model_validate_json(row[0])


def save_project(p: Project, *, edit=False, expected_revision=None):
    with LOCK, connection() as c:
        row = c.execute('SELECT data FROM projects WHERE id=?', (p.id,)).fetchone()
        if row:
            previous = Project.model_validate_json(row[0])
            if expected_revision is not None and previous.revision != expected_revision:
                raise ValueError('다른 변경 사항이 있습니다. 프로젝트를 다시 열어 주세요.')
            if edit:
                c.execute('INSERT OR REPLACE INTO history VALUES (?,?,?)', (p.id, previous.revision, row[0]))
                p.revision = previous.revision + 1
            else:
                p.revision = previous.revision
        p.updated_at = now()
        p.created_at = p.created_at or p.updated_at
        c.execute('INSERT OR REPLACE INTO projects VALUES (?,?)', (p.id, p.model_dump_json()))
    return p


def list_projects():
    with LOCK, connection() as c:
        rows = c.execute('SELECT data FROM projects').fetchall()
    projects = [json.loads(row[0]) for row in rows]
    return sorted([{
        **{k: p[k] for k in ['id', 'title', 'status', 'stage', 'progress', 'updated_at', 'duration', 'video_id', 'source']},
        'note_count': len(p['notes']), 'review_count': sum(n['confidence'] < .65 and not n['reviewed'] for n in p['notes'])
    } for p in projects], key=lambda x: x['updated_at'], reverse=True)


def project_dir(pid):
    import re
    if not re.fullmatch(r'[a-f0-9]{32}', pid):
        raise ValueError('잘못된 프로젝트 ID입니다.')
    folder = DATA / 'projects' / pid
    folder.mkdir(exist_ok=True)
    return folder


def delete_project(pid: str):
    """Remove the project, its edit history, and its media/analysis folder."""
    folder = project_dir(pid)
    with LOCK, connection() as c:
        if not c.execute('DELETE FROM projects WHERE id=?', (pid,)).rowcount:
            raise KeyError(pid)
        c.execute('DELETE FROM history WHERE project_id=?', (pid,))
    shutil.rmtree(folder, ignore_errors=True)
