"""FastAPI application factory (spec section 14).

Exposes the OpenAI-compatible chat endpoint so Open WebUI can use the PT
as its backend. Open WebUI remains the UI; all training logic lives here.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("pt.log"),
    ],
)

from personal_trainer.config import Settings, get_settings
from personal_trainer.integrations.llm import OpenRouterClient
from personal_trainer.programmes.manager import ProgrammeManager
from personal_trainer.storage.drive import GoogleDriveStorageBackend
from personal_trainer.storage.local import LocalStorageBackend

from .chat import PersonalTrainer

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    # --- storage backend (spec section 6) --------------------------------
    if settings.storage.backend == "drive":
        storage = GoogleDriveStorageBackend(settings.storage)
    else:
        storage = LocalStorageBackend(settings.storage.root)

    programme_manager = ProgrammeManager(
        os.environ.get("PT_PROGRAMMES_CONFIG", str(PROJECT_ROOT / "config" / "programmes.yaml"))
    )
    llm = OpenRouterClient(settings.llm)
    pt = PersonalTrainer(settings, storage, llm, programme_manager)

    app = FastAPI(title="Personal Trainer", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "storage": settings.storage.backend, "model": settings.llm.model}

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        body = await request.json()
        messages = body.get("messages") or []
        if not messages:
            return JSONResponse({"error": {"message": "messages required"}}, status_code=400)

        try:
            if body.get("stream"):
                return StreamingResponse(
                    pt.stream_completion(messages),
                    media_type="text/event-stream",
                )
            return JSONResponse(pt.handle_completion(messages))
        except RuntimeError as exc:
            # e.g. missing API key — report as a clean 502, not a 500.
            return JSONResponse(
                {"error": {"message": str(exc), "type": "server_error"}},
                status_code=502,
            )

    @app.get("/v1/models")
    def models() -> dict:
        """Minimal model listing so Open WebUI can enumerate the backend."""
        return {
            "object": "list",
            "data": [{
                "id": "personal-trainer",
                "object": "model",
                "created": 0,
                "owned_by": "personal-trainer",
            }],
        }

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
