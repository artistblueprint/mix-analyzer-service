# analyze_service.py
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import traceback
import os

import mix_client  # local module (the file below)

app = FastAPI(title="Mix Analyzer Service", version="1.0.0")

# Optional CORS for your Base44 frontend (adjust origins as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True, "base": os.getenv("MIX_BASE_URL", "https://mixanalytic.com")}

@app.post("/analyze")
async def analyze(
    song: UploadFile = File(..., description="Audio file (mp3/wav/flac/etc.)"),
    instrumental: bool = Form(False),
    timeout: int = Form(180)  # seconds to wait for remote results/json
):
    """
    Receives a file upload and forwards to mixanalytic.com.
    Returns either full JSON results (if ready) or a visuals-only payload as a fallback.
    """
    try:
        audio_bytes = await song.read()
        if not audio_bytes:
            raise ValueError("Uploaded file is empty")

        # Call your working client (refactored from auto_analyze.py)
        result = mix_client.analyze_track(
            file_bytes=audio_bytes,
            filename=song.filename or "upload.mp3",
            is_instrumental=instrumental,
            timeout=timeout,
            retry_csrf=True
        )
        return JSONResponse(result)

    except Exception as e:
        # Log full traceback to the server console
        traceback.print_exc()
        # Surface readable details to the client for debugging
        raise HTTPException(
            status_code=500,
            detail=f"{type(e).__name__}: {str(e)}"
        )