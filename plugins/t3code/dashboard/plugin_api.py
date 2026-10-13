"""T3 Code REST backend, mounted by Hermes at ``/api/plugins/t3code/``.

Every feature's routes live in ``features/<key>/api.py`` and are mounted under ``/<key>``:

  /inbox/…         features/inbox/api.py
  /usage/…         features/usage/api.py
  /orchestrator/…  features/orchestrator/api.py

Isolation: each feature module is imported on its own. One that fails to import answers 503 with
its error on its own prefix while the other features mount normally; one switched off in the
plugin settings answers 404 (checked per request, so toggles apply without a restart).

  GET  /features   toggles + per-feature status (Desktop shell, settings page)
  POST /features   {"<feature>": bool, …} — writes the same settings the Hermes plugin manager does
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

logger = logging.getLogger("t3code")
ROOT = Path(__file__).resolve().parent.parent


def _load_shared():
    spec = importlib.util.spec_from_file_location("_t3code_shared_boot", ROOT / "shared" / "__init__.py")
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    boot.load(ROOT)
    return importlib.import_module("t3code_shared.features")


features = _load_shared()
router = APIRouter()


def _require(key: str):
    def check() -> None:
        if not features.feature_enabled(key):
            raise HTTPException(status_code=404, detail={"error": "feature_disabled", "feature": key,
                                                         "message": f"T3 Code {features.FEATURES[key].label} is switched off"})
    return check


def _failed_router(key: str, error: str) -> APIRouter:
    broken = APIRouter()

    @broken.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unavailable(path: str) -> None:  # noqa: ARG001
        raise HTTPException(status_code=503, detail={"error": "feature_failed", "feature": key, "message": error})

    return broken


def _mount(key: str) -> None:
    sub = None

    def load() -> None:
        nonlocal sub
        sub = features.load_feature_module(key, "api.py").router

    if features.guarded(key, "api", load):
        router.include_router(sub, prefix=f"/{key}", dependencies=[Depends(_require(key))])
    else:
        error = features.status_snapshot().get(key, {}).get("api", {}).get("error") or "failed to load"
        router.include_router(_failed_router(key, error), prefix=f"/{key}")


for _feature in features.FEATURES.values():
    if _feature.api:
        _mount(_feature.key)


def _features_payload() -> Dict[str, Any]:
    toggles = features.current_toggles()
    status = features.status_snapshot()
    return {
        "features": [
            {"key": f.key, "label": f.label, "enabled": toggles.get(f.key, True), "restart": f.restart,
             "status": status.get(f.key, {})}
            for f in features.FEATURES.values()
        ],
    }


@router.get("/features")
async def get_features() -> Dict[str, Any]:
    return _features_payload()


@router.post("/features")
async def post_features(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    values = {k: v for k, v in (body or {}).items() if k in features.FEATURES}
    if not values or not all(isinstance(v, bool) for v in values.values()):
        raise HTTPException(status_code=400, detail={"error": "bad_request",
                                                     "message": "expected {feature: true|false}"})
    from hermes_cli.plugins_settings import save_plugin_settings
    try:
        save_plugin_settings(features.PLUGIN_ID, ROOT, values)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail={"error": "not_saved", "message": str(exc)})
    return _features_payload()
