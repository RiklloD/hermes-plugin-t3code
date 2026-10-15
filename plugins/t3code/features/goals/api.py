"""Provider-independent persisted goal readout (no writes or agent turns)."""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException

from t3code_shared.features import load_feature_module

read_goal = load_feature_module("goals", "state.py").read_goal

router = APIRouter()


@router.get("/sessions/{session_id}")
def get_goal(session_id: str, profile: Optional[str] = None):
    from hermes_cli.web_server_sessions import _session_db_path_for_profile
    if profile == "all":
        raise HTTPException(400, "A goal belongs to one profile")
    path = _session_db_path_for_profile(profile)
    if not path.is_file():
        raise HTTPException(404, "Session not found")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            goal = read_goal(db, session_id)
    except LookupError:
        raise HTTPException(404, "Session not found") from None
    finally:
        db.close()
    return {"session_id": session_id, "goal": goal}
