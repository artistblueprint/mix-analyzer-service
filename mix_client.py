# mix_client.py
import os
import time
import json
import mimetypes
from typing import Dict, Any, Optional

import requests


# ===== Config =====
BASE = os.getenv("MIX_BASE_URL", "https://mixanalytic.com")
UPLOAD_PATH = os.getenv("MIX_UPLOAD_PATH", "/upload")  # set to "/api/upload" if the site changes
RESULTS_JSON_PATH = os.getenv("MIX_RESULTS_PATH", "/api/results/{file_id}.json")

# Generic headers used for GETs (csrf + polling)
BASE_HEADERS = {
    "User-Agent": "MixAnalyzerService/1.0 (+python; FastAPI client)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
}


def _guess_mimetype(filename: str) -> str:
    mt, _ = mimetypes.guess_type(filename)
    return mt or "audio/mpeg"


def _new_session() -> requests.Session:
    """
    Create a session with browser-ish defaults and keep-alive.
    """
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    # Keep-Alive helps with some CDNs
    s.headers.update({"Connection": "keep-alive"})
    return s


def _get_csrf(session: requests.Session, timeout: int = 20) -> str:
    """
    Fetch CSRF token from /csrf-token.
    Accepts both {"csrf_token": "..."} or {"csrfToken": "..."}.
    """
    url = f"{BASE}/csrf-token"
    r = session.get(url, headers={**BASE_HEADERS, "Accept": "application/json"}, timeout=timeout)
    r.raise_for_status()

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Could not parse CSRF response as JSON: {r.text[:300]}")

    token = data.get("csrf_token") or data.get("csrfToken")
    if not token:
        raise RuntimeError(f"Could not fetch CSRF token: payload keys={list(data.keys())}")

    # Some backends expect X-CSRFToken header echo on upload; stash it on the session
    session.headers["X-CSRFToken"] = token
    return token


def _browser_like_post_headers(csrf_token: str) -> Dict[str, str]:
    """
    Headers that mimic the site’s own AJAX upload:
    - Origin/Referer
    - X-Requested-With: XMLHttpRequest
    - X-CSRFToken: <token>
    """
    return {
        "Origin": BASE,
        "Referer": f"{BASE}/",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrf_token,
        "Accept": "application/json, text/plain, */*",
    }


def _post_with_optional_retry(
    session: requests.Session,
    url: str,
    files: Dict[str, Any],
    data: Dict[str, Any],
    headers: Dict[str, str],
    timeout: int,
    retry_csrf: bool,
) -> requests.Response:
    """
    POST with optional CSRF refresh retry and simple 429/500 backoff.
    """
    def do_post() -> requests.Response:
        return session.post(url, files=files, data=data, headers=headers, timeout=timeout)

    r = do_post()

    # Unauthorized/Forbidden: CSRF may have rotated. Refresh once.
    if r.status_code in (401, 403) and retry_csrf:
        try:
            new_csrf = _get_csrf(session, timeout=min(20, timeout))
            headers.update(_browser_like_post_headers(new_csrf))
            data["csrf_token"] = new_csrf
            r = do_post()
        except Exception:
            pass

    # Transient overloads or rate-limits: brief backoff and retry once.
    if r.status_code in (429, 500, 502, 503, 504):
        time.sleep(2)
        r = do_post()

    return r


def _poll_json_results(session: requests.Session, file_id: str, timeout: int) -> Optional[Dict[str, Any]]:
    """
    Poll for final JSON results file.
    """
    json_url = f"{BASE}{RESULTS_JSON_PATH.format(file_id=file_id)}"
    start = time.time()
    while time.time() - start < timeout:
        r = session.get(json_url, timeout=15)
        if r.status_code == 200:
            ctype = r.headers.get("content-type", "")
            if ctype.startswith("application/json"):
                try:
                    return r.json()
                except Exception:
                    time.sleep(2)
                    continue
        elif r.status_code in (404, 403):
            time.sleep(3)
            continue
        else:
            time.sleep(2)
    return None


def _visuals_fallback(file_id: str, filename: str) -> Dict[str, Any]:
    static = f"{BASE}/static/uploads/{file_id}"
    return {
        "file_id": file_id,
        "filename": filename,
        "status": "visuals_ready_only",
        "visualizations": {
            "waveform":      f"{static}/waveform.png",
            "spectrogram":   f"{static}/spectrogram.png",
            "spectrum":      f"{static}/spectrum.png",
            "chromagram":    f"{static}/chromagram.png",
            "stereo_field":  f"{static}/stereo_field.png",
            "vectorscope":   f"{static}/vectorscope.png",
            "dynamic_range": f"{static}/dynamic_range.png",
            "spatial_field": f"{static}/spatial_field.png",
        }
    }


def analyze_track(
    file_bytes: bytes,
    filename: str,
    is_instrumental: bool = False,
    timeout: int = 180,
    retry_csrf: bool = True
) -> Dict[str, Any]:
    """
    Uploads an audio file to mixanalytic.com and returns:
      - full JSON results if available,
      - or visuals-only payload as a fallback.
    """
    session = _new_session()

    # 1) CSRF
    csrf = _get_csrf(session, timeout=min(20, timeout))
    post_headers = _browser_like_post_headers(csrf)

    # 2) Upload
    upload_url = f"{BASE}{UPLOAD_PATH}"
    files = {
        # The site expects the field name "file"
        "file": (filename, file_bytes, _guess_mimetype(filename)),
    }
    # Send both keys just in case the backend accepts either
    data = {
        "csrf_token": csrf,
        "is_instrumental": str(bool(is_instrumental)).lower(),
        "instrumental": str(bool(is_instrumental)).lower(),
    }

    r = _post_with_optional_retry(
        session=session,
        url=upload_url,
        files=files,
        data=data,
        headers=post_headers,
        timeout=timeout,
        retry_csrf=retry_csrf,
    )

    if r.status_code != 200:
        text = r.text
        # include a bigger slice so we can see real error messages
        if len(text) > 4000:
            text = text[:4000] + "…"
        raise RuntimeError(f"Upload failed ({r.status_code}): {text}")

    try:
        resp = r.json()
    except Exception:
        raise RuntimeError(f"Upload returned non-JSON response: {r.text[:500]}")

    # 3) If cached, you get results immediately
    if resp.get("results"):
        return resp

    # 4) Async path: must have file_id
    file_id = resp.get("file_id")
    if not file_id:
        raise RuntimeError(f"No file_id in upload response: {json.dumps(resp)[:500]}")

    # 5) Poll for final JSON
    json_results = _poll_json_results(session, file_id=file_id, timeout=timeout)
    if json_results:
        json_results.setdefault("file_id", file_id)
        json_results.setdefault("filename", filename)
        return json_results

    # 6) Fallback to visuals (usually ready faster)
    return _visuals_fallback(file_id=file_id, filename=filename)