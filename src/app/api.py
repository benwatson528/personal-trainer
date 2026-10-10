"""FastAPI application factory (spec section 14).

Exposes the OpenAI-compatible chat endpoint so Open WebUI can use the PT
as its backend. Open WebUI remains the UI; all training logic lives here.
"""

from __future__ import annotations

import logging
import os

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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    # --- storage backend (spec section 6) --------------------------------
    if settings.storage.backend in ("gdrive", "drive"):
        storage = GoogleDriveStorageBackend(settings.storage)
    else:
        storage = LocalStorageBackend(settings.storage.root)

    # Programmes are content config, read from the storage root (Drive) so the
    # repo carries none of it. PT_PROGRAMMES_CONFIG points at a local file for
    # development and tests.
    override = os.environ.get("PT_PROGRAMMES_CONFIG")
    if override:
        programme_manager = ProgrammeManager.from_file(override)
    else:
        programme_manager = ProgrammeManager.from_storage(
            storage, settings.folders.programmes_config
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

    # Bind is configurable so the service can be locked to localhost. It defaults
    # to all interfaces because the bundled Open WebUI runs in Docker and reaches
    # the host over the bridge; there is no auth on the API, so anything that can
    # reach this port can drive the LLM and the athlete's Drive. Set
    # PT_HOST=127.0.0.1 when something else (a reverse proxy) fronts it.
    host = os.environ.get("PT_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    logging.getLogger("personal_trainer").info("Serving on %s:%s", host, port)
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
