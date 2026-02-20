"""
Pod sidecar handler.

Exposes a small FastAPI app that:
  - On startup: polls ComfyUI until ready, then fires a 'ready' callback
  - POST /run  : accepts a job, runs it through ComfyUI, fires a completion/failure callback
  - GET /health: simple liveness check
"""
import asyncio
import base64
import json
import os
import shutil
import time
import traceback
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any, Dict, List, Optional

import requests
import uvicorn
import websocket
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMFY_HOST = "127.0.0.1:8188"
SIDECAR_PORT = int(os.environ.get("SIDECAR_PORT", "8189"))
SIDECAR_API_KEY = os.environ.get("SIDECAR_API_KEY", "")
POD_ID = os.environ.get("RUNPOD_POD_ID", os.environ.get("POD_ID", "unknown"))

COMFY_READY_MAX_RETRIES = int(os.environ.get("COMFY_READY_MAX_RETRIES", "600"))
COMFY_READY_INTERVAL_S = float(os.environ.get("COMFY_READY_INTERVAL_S", "1.0"))

WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", "5"))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", "3"))

# ---------------------------------------------------------------------------
# Startup: poll ComfyUI, fire ready callback
# ---------------------------------------------------------------------------

def _wait_for_comfy() -> bool:
    url = f"http://{COMFY_HOST}/"
    print(f"[sidecar] Waiting for ComfyUI at {url}...")
    for attempt in range(COMFY_READY_MAX_RETRIES):
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                print(f"[sidecar] ComfyUI ready after {attempt + 1} attempts")
                return True
        except Exception:
            pass
        time.sleep(COMFY_READY_INTERVAL_S)
    print(f"[sidecar] ComfyUI did not become ready after {COMFY_READY_MAX_RETRIES} attempts")
    return False


def _fire_callback(url: str, payload: dict, label: str = "callback"):
    """POST payload to url with basic retry. Best-effort, never raises."""
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code < 300:
                print(f"[sidecar] {label} delivered (attempt {attempt + 1})")
                return
            print(f"[sidecar] {label} got {r.status_code}, retrying...")
        except Exception as e:
            print(f"[sidecar] {label} attempt {attempt + 1} failed: {e}")
        time.sleep(5)
    print(f"[sidecar] {label} failed after 3 attempts")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    ready_callback_url = os.environ.get("READY_CALLBACK_URL", "")

    async def _startup():
        ok = await loop.run_in_executor(None, _wait_for_comfy)
        if ready_callback_url:
            payload = {"event": "ready", "pod_id": POD_ID, "success": ok}
            await loop.run_in_executor(None, _fire_callback, ready_callback_url, payload, "ready-callback")
        else:
            print("[sidecar] No READY_CALLBACK_URL set, skipping ready callback")

    asyncio.create_task(_startup())
    yield


app = FastAPI(title="Lovegood Pod Sidecar", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth(authorization: Optional[str]):
    if not SIDECAR_API_KEY:
        return
    if authorization != f"Bearer {SIDECAR_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    workflow: Dict[str, Any]
    callback_url: str
    images: Optional[List[Dict[str, str]]] = None
    download_urls: Optional[List[Dict[str, str]]] = None
    upload_urls: Optional[List[Dict[str, str]]] = None
    comfy_org_api_key: Optional[str] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    comfy_ok = False
    try:
        r = requests.get(f"http://{COMFY_HOST}/", timeout=3)
        comfy_ok = r.status_code == 200
    except Exception:
        pass
    return {"status": "ok", "pod_id": POD_ID, "comfy_ready": comfy_ok}


@app.post("/run", status_code=202)
async def run(request: RunRequest, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    job_id = str(uuid.uuid4())
    print(f"[sidecar] Accepted job {job_id}")
    asyncio.create_task(_run_job(job_id, request))
    return {"job_id": job_id, "status": "accepted"}

# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

async def _run_job(job_id: str, req: RunRequest):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _execute_job, job_id, req)
    _fire_callback(req.callback_url, {"job_id": job_id, **result}, "job-callback")


def _execute_job(job_id: str, req: RunRequest) -> dict:
    try:
        _cleanup_comfyui_directories()

        if not _check_server(f"http://{COMFY_HOST}/"):
            return {"status": "failed", "error": "ComfyUI not reachable"}

        if req.images:
            r = _upload_images(req.images)
            if r["status"] == "error":
                return {"status": "failed", "error": "Image upload failed", "details": r["details"]}

        if req.download_urls:
            r = _download_and_upload_files(req.download_urls)
            if r["status"] == "error":
                return {"status": "failed", "error": "File download/upload failed", "details": r["details"]}

        client_id = str(uuid.uuid4())
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)

        try:
            queued = _queue_workflow(req.workflow, client_id, req.comfy_org_api_key)
            prompt_id = queued.get("prompt_id")
            if not prompt_id:
                return {"status": "failed", "error": f"No prompt_id in response: {queued}"}
            print(f"[sidecar] Job {job_id} -> prompt {prompt_id}")
            execution_done, exec_errors = _monitor_execution(ws, ws_url, prompt_id)
        finally:
            if ws.connected:
                ws.close()

        if not execution_done and not exec_errors:
            return {"status": "failed", "error": "Execution monitoring exited unexpectedly"}

        history = _get_history(prompt_id)
        if prompt_id not in history:
            return {"status": "failed", "error": f"Prompt {prompt_id} not found in history"}

        outputs = history[prompt_id].get("outputs", {})
        output_data, output_errors = _process_outputs(outputs, req.upload_urls)
        all_errors = exec_errors + output_errors

        if not output_data and all_errors:
            return {"status": "failed", "error": "Job produced no output", "details": all_errors}

        result = {"status": "completed", "images": output_data}
        if all_errors:
            result["warnings"] = all_errors
        return result

    except Exception as e:
        traceback.print_exc()
        return {"status": "failed", "error": str(e)}

# ---------------------------------------------------------------------------
# ComfyUI helpers
# ---------------------------------------------------------------------------

def _check_server(url: str, retries: int = 5, delay: float = 1.0) -> bool:
    for _ in range(retries):
        try:
            if requests.get(url, timeout=5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def _cleanup_comfyui_directories():
    preserve = {"/comfyui/input/demo"}
    for directory in ["/comfyui/input", "/comfyui/output", "/comfyui/temp"]:
        if not os.path.exists(directory):
            continue
        for item in os.listdir(directory):
            item_path = os.path.join(directory, item)
            if item_path in preserve:
                continue
            try:
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                print(f"[sidecar] Cleanup warning: {item_path}: {e}")


def _upload_images(images: list) -> dict:
    errors = []
    for image in images:
        try:
            name = image["name"]
            data = image["image"]
            if "," in data:
                data = data.split(",", 1)[1]
            blob = base64.b64decode(data)
            files = {"image": (name, BytesIO(blob), "image/png"), "overwrite": (None, "true")}
            requests.post(f"http://{COMFY_HOST}/upload/image", files=files, timeout=30).raise_for_status()
        except Exception as e:
            errors.append(f"Failed to upload {image.get('name')}: {e}")
    return {"status": "error" if errors else "success", "details": errors}


def _download_and_upload_files(download_urls: list) -> dict:
    errors = []
    for item in download_urls:
        try:
            name = item["name"]
            file_bytes = requests.get(item["url"], timeout=120).content
            subfolder, filename = (name.rsplit("/", 1) if "/" in name else ("", name))
            name_lower = filename.lower()

            if name_lower.endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')):
                endpoint = f"http://{COMFY_HOST}/upload/video"
                content_type, form_field = "video/mp4", "video"
            else:
                endpoint = f"http://{COMFY_HOST}/upload/image"
                content_type, form_field = "image/png", "image"

            files = {form_field: (filename, BytesIO(file_bytes), content_type), "overwrite": (None, "true")}
            if subfolder:
                files["subfolder"] = (None, subfolder)
            requests.post(endpoint, files=files, timeout=60).raise_for_status()
        except Exception as e:
            errors.append(f"Failed to process {item.get('name')}: {e}")
    return {"status": "error" if errors else "success", "details": errors}


def _queue_workflow(workflow: dict, client_id: str, comfy_org_api_key: Optional[str] = None) -> dict:
    payload = {"prompt": workflow, "client_id": client_id}
    effective_key = comfy_org_api_key or os.environ.get("COMFY_ORG_API_KEY")
    if effective_key:
        payload["extra_data"] = {"api_key_comfy_org": effective_key}
    r = requests.post(
        f"http://{COMFY_HOST}/prompt",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code == 400:
        raise ValueError(f"Workflow validation failed: {r.text}")
    r.raise_for_status()
    return r.json()


def _monitor_execution(ws: websocket.WebSocket, ws_url: str, prompt_id: str):
    errors = []
    while True:
        try:
            out = ws.recv()
            if not isinstance(out, str):
                continue
            msg = json.loads(out)
            msg_type = msg.get("type")

            if msg_type == "executing":
                data = msg.get("data", {})
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    return True, errors

            elif msg_type == "execution_error":
                data = msg.get("data", {})
                if data.get("prompt_id") == prompt_id:
                    errors.append(
                        f"Node {data.get('node_id')} ({data.get('node_type')}): {data.get('exception_message')}"
                    )
                    return False, errors

        except websocket.WebSocketTimeoutException:
            continue
        except websocket.WebSocketConnectionClosedException as e:
            ws = _reconnect_websocket(ws_url, e)
        except json.JSONDecodeError:
            continue


def _reconnect_websocket(ws_url: str, initial_error: Exception) -> websocket.WebSocket:
    for attempt in range(WEBSOCKET_RECONNECT_ATTEMPTS):
        try:
            requests.get(f"http://{COMFY_HOST}/", timeout=5).raise_for_status()
        except Exception:
            raise websocket.WebSocketConnectionClosedException("ComfyUI unreachable during reconnect")
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print(f"[sidecar] WebSocket reconnected on attempt {attempt + 1}")
            return new_ws
        except Exception as e:
            print(f"[sidecar] Reconnect attempt {attempt + 1} failed: {e}")
            time.sleep(WEBSOCKET_RECONNECT_DELAY_S)
    raise websocket.WebSocketConnectionClosedException(
        f"Failed to reconnect after {WEBSOCKET_RECONNECT_ATTEMPTS} attempts"
    )


def _get_history(prompt_id: str) -> dict:
    r = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    r.raise_for_status()
    return r.json()


def _process_outputs(outputs: dict, upload_urls: Optional[List[Dict[str, str]]]) -> tuple:
    upload_map = {item["name"]: item["url"] for item in (upload_urls or [])}
    output_data, errors = [], []

    for node_id, node_output in outputs.items():
        for key in ["videos", "gifs", "images"]:
            for file_info in node_output.get(key, []):
                filename = file_info.get("filename")
                subfolder = file_info.get("subfolder", "")
                file_type = file_info.get("type")

                if not filename or file_type == "temp":
                    continue

                params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": file_type})
                try:
                    file_bytes = requests.get(f"http://{COMFY_HOST}/view?{params}", timeout=60).content
                except Exception as e:
                    errors.append(f"Failed to fetch {filename}: {e}")
                    continue

                upload_url = upload_map.get(filename)
                if upload_url:
                    try:
                        content_type = "video/mp4" if filename.endswith(('.mp4', '.mov', '.avi')) else "image/png"
                        requests.put(upload_url, data=file_bytes, headers={"Content-Type": content_type}, timeout=60).raise_for_status()
                        output_data.append({"filename": filename, "type": "uploaded"})
                    except Exception as e:
                        errors.append(f"Failed to upload {filename}: {e}")
                else:
                    output_data.append({
                        "filename": filename,
                        "type": "base64",
                        "data": base64.b64encode(file_bytes).decode()
                    })

    return output_data, errors


if __name__ == "__main__":
    print(f"[sidecar] Starting on port {SIDECAR_PORT}, pod_id={POD_ID}")
    uvicorn.run(app, host="0.0.0.0", port=SIDECAR_PORT, log_level="warning")
