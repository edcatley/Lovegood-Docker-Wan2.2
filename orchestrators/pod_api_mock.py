"""
Mock RunPod Pod REST API

Mimics the subset of https://rest.runpod.io/v1/pods we actually use:
  POST   /pods          - create + start a pod (spins up a local Docker container)
  GET    /pods          - list all pods
  GET    /pods/{id}     - get a single pod
  POST   /pods/{id}/stop    - stop a pod (pause the container)
  DELETE /pods/{id}     - terminate a pod (remove the container)

The response shapes match the real RunPod API so your pod_manager can
swap between mock and real by just changing the base URL.

Env vars:
  MOCK_API_KEY      - Bearer token required on all requests (default: "test-key")
  MOCK_IMAGE        - Docker image to run (default: "lovegood-comfyui:latest")
  MODELS_PATH       - Host path to mount at /workspace inside the container
  SIDECAR_PORT      - Port the sidecar listens on inside the container (default: 8189)
"""
import asyncio
import json
import os
import threading
import time
import uuid

import docker
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MOCK_API_KEY = os.environ.get("MOCK_API_KEY", "test-key")
MOCK_IMAGE = os.environ.get("MOCK_IMAGE", "lovegood-comfyui:latest")
MODELS_PATH = os.environ.get("MODELS_PATH", "")
SIDECAR_PORT = int(os.environ.get("SIDECAR_PORT", "8189"))
MOCK_API_PORT = int(os.environ.get("MOCK_API_PORT", "9000"))

docker_client = docker.from_env()

# In-memory pod registry: pod_id -> pod record
_pods: Dict[str, dict] = {}
_lock = threading.Lock()

app = FastAPI(title="RunPod Pod API Mock")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth(authorization: Optional[str]):
    if authorization != f"Bearer {MOCK_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pod_response(pod: dict) -> dict:
    """Shape a pod record into the RunPod API response format."""
    return {
        "id": pod["id"],
        "name": pod["name"],
        "desiredStatus": pod["desiredStatus"],
        "image": pod["image"],
        "env": pod.get("env", {}),
        "ports": pod.get("ports", []),
        "portMappings": pod.get("portMappings", {}),
        "containerDiskInGb": pod.get("containerDiskInGb", 50),
        "volumeMountPath": "/workspace",
        "lastStartedAt": pod.get("lastStartedAt"),
        "lastStatusChange": pod.get("lastStatusChange"),
        # Stub machine/gpu fields so callers don't have to null-check
        "machine": {"gpuTypeId": "LOCAL_DOCKER", "location": "local"},
        "gpu": {"id": "LOCAL", "count": 1, "displayName": "Local Docker"},
        "costPerHr": "0.00",
        "adjustedCostPerHr": 0.0,
        # Internal: container id for our own use
        "_containerId": pod.get("containerId"),
        "_sidecarUrl": pod.get("sidecarUrl"),
    }


def _start_container(pod_id: str, image: str, env: dict, name: str) -> dict:
    """
    Spin up a Docker container for this pod.
    Returns updated fields to merge into the pod record.
    """
    container_env = {**env, "POD_ID": pod_id, "RUNPOD_POD_ID": pod_id}

    volumes = {}
    if MODELS_PATH and os.path.exists(MODELS_PATH):
        volumes[MODELS_PATH] = {"bind": "/workspace", "mode": "rw"}

    # Detect GPU
    device_requests = None
    try:
        docker_client.containers.run(
            "nvidia/cuda:11.8.0-base-ubuntu22.04", "nvidia-smi",
            remove=True,
            device_requests=[docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
        )
        device_requests = [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
        print(f"[mock-api] GPU available for pod {pod_id}")
    except Exception:
        print(f"[mock-api] No GPU, running CPU-only for pod {pod_id}")

    run_kwargs = {
        "image": image,
        "environment": container_env,
        "detach": True,
        "remove": False,
        "volumes": volumes or None,
        "ports": {f"{SIDECAR_PORT}/tcp": None},  # random host port
        "name": f"mock-pod-{pod_id[:8]}",
    }
    if device_requests:
        run_kwargs["device_requests"] = device_requests

    container = docker_client.containers.run(**run_kwargs)
    print(f"[mock-api] Container {container.short_id} started for pod {pod_id}")

    # Resolve the randomly assigned host port for the sidecar
    container.reload()
    host_port = container.ports.get(f"{SIDECAR_PORT}/tcp")
    sidecar_url = None
    if host_port:
        port_num = host_port[0]["HostPort"]
        sidecar_url = f"http://host.docker.internal:{port_num}"
        print(f"[mock-api] Sidecar reachable at {sidecar_url}")

    return {
        "containerId": container.id,
        "sidecarUrl": sidecar_url,
        "portMappings": {str(SIDECAR_PORT): int(port_num) if host_port else None},
        "lastStartedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastStatusChange": f"Started by mock API at {time.strftime('%c')}",
        "desiredStatus": "RUNNING",
    }

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreatePodRequest(BaseModel):
    name: Optional[str] = None
    image: Optional[str] = None
    gpuTypeId: Optional[str] = None
    containerDiskInGb: Optional[int] = 50
    volumeInGb: Optional[int] = 20
    ports: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    networkVolumeId: Optional[str] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "pods": len(_pods)}


@app.post("/pods", status_code=201)
async def create_pod(body: CreatePodRequest, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)

    pod_id = str(uuid.uuid4())[:14].replace("-", "")
    image = body.image or MOCK_IMAGE
    name = body.name or f"mock-pod-{pod_id[:8]}"
    env = body.env or {}

    pod = {
        "id": pod_id,
        "name": name,
        "image": image,
        "env": env,
        "ports": body.ports or [f"{SIDECAR_PORT}/http"],
        "containerDiskInGb": body.containerDiskInGb,
        "desiredStatus": "STARTING",
        "lastStartedAt": None,
        "lastStatusChange": f"Created by mock API at {time.strftime('%c')}",
        "containerId": None,
        "sidecarUrl": None,
        "portMappings": {},
    }

    with _lock:
        _pods[pod_id] = pod

    # Start container in background so we return quickly (like the real API)
    def _boot():
        try:
            updates = _start_container(pod_id, image, env, name)
            with _lock:
                _pods[pod_id].update(updates)
        except Exception as e:
            print(f"[mock-api] Failed to start container for pod {pod_id}: {e}")
            with _lock:
                _pods[pod_id]["desiredStatus"] = "EXITED"
                _pods[pod_id]["lastStatusChange"] = f"Boot failed: {e}"

    threading.Thread(target=_boot, daemon=True).start()

    return _make_pod_response(pod)


@app.get("/pods")
async def list_pods(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    with _lock:
        return [_make_pod_response(p) for p in _pods.values()]


@app.get("/pods/{pod_id}")
async def get_pod(pod_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    with _lock:
        pod = _pods.get(pod_id)
    if not pod:
        raise HTTPException(status_code=404, detail=f"Pod {pod_id} not found")

    # Sync desiredStatus from actual container state
    if pod.get("containerId"):
        try:
            container = docker_client.containers.get(pod["containerId"])
            container.reload()
            status_map = {"running": "RUNNING", "exited": "EXITED", "paused": "PAUSED", "created": "STARTING"}
            pod["desiredStatus"] = status_map.get(container.status, container.status.upper())
        except docker.errors.NotFound:
            pod["desiredStatus"] = "EXITED"

    return _make_pod_response(pod)


@app.post("/pods/{pod_id}/stop")
async def stop_pod(pod_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    with _lock:
        pod = _pods.get(pod_id)
    if not pod:
        raise HTTPException(status_code=404, detail=f"Pod {pod_id} not found")

    if pod.get("containerId"):
        try:
            container = docker_client.containers.get(pod["containerId"])
            container.stop(timeout=10)
            print(f"[mock-api] Stopped container for pod {pod_id}")
        except docker.errors.NotFound:
            pass
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to stop container: {e}")

    with _lock:
        _pods[pod_id]["desiredStatus"] = "EXITED"
        _pods[pod_id]["lastStatusChange"] = f"Stopped at {time.strftime('%c')}"

    return _make_pod_response(_pods[pod_id])


@app.delete("/pods/{pod_id}", status_code=204)
async def terminate_pod(pod_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    with _lock:
        pod = _pods.pop(pod_id, None)
    if not pod:
        raise HTTPException(status_code=404, detail=f"Pod {pod_id} not found")

    if pod.get("containerId"):
        try:
            container = docker_client.containers.get(pod["containerId"])
            container.remove(force=True)
            print(f"[mock-api] Removed container for pod {pod_id}")
        except docker.errors.NotFound:
            pass
        except Exception as e:
            print(f"[mock-api] Warning: could not remove container for pod {pod_id}: {e}")

    return JSONResponse(status_code=204, content=None)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[mock-api] RunPod Pod API mock starting on port {MOCK_API_PORT}")
    print(f"[mock-api] Image: {MOCK_IMAGE}")
    print(f"[mock-api] Auth key: {MOCK_API_KEY}")
    uvicorn.run(app, host="0.0.0.0", port=MOCK_API_PORT, log_level="warning")
