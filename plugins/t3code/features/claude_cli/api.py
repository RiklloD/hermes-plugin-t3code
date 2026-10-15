"""Native Claude account metadata; authentication stays in the official CLI."""
from __future__ import annotations

import asyncio
from fastapi import APIRouter
from t3code_shared.features import load_feature_module

router = APIRouter()


@router.get("/accounts")
async def get_accounts() -> dict:
    opts = load_feature_module("claude_cli", "options.py")
    accounts = opts.account_definitions()
    statuses = await asyncio.gather(*(
        asyncio.to_thread(opts.account_status, account["settings"]) for account in accounts
    ))
    return {"accounts": [
        {"id": account["id"], "label": account["label"], **status}
        for account, status in zip(accounts, statuses)
    ]}
