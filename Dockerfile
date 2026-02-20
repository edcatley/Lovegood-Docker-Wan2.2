# 1. SETUP THE BASE (CUDA 13.0)
ARG BASE_IMAGE=nvidia/cuda:13.0.0-devel-ubuntu24.04
FROM ${BASE_IMAGE} AS base

# 2. SETUP ARGS
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
ARG COMFYUI_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_PREFER_BINARY=1 \
    PYTHONUNBUFFERED=1 \
    TORCH_CUDA_ARCH_LIST="8.9;9.0;10.0" \
    CMAKE_BUILD_PARALLEL_LEVEL=8 \
    UV_HTTP_TIMEOUT=600

# 3. INSTALL SYSTEM DEPS
RUN apt-get update && apt-get install -y \
    python3.12 python3.12-venv python3.12-dev \
    git wget build-essential ninja-build ffmpeg \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 4. INSTALL PYTHON ENV
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && uv venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# 5. INSTALL COMFY & PYTORCH
RUN uv pip install comfy-cli pip setuptools wheel ninja
RUN uv pip install --no-cache-dir torch torchvision --index-url ${PYTORCH_INDEX_URL}
RUN /usr/bin/yes | comfy --workspace /comfyui install --version "${COMFYUI_VERSION}" --nvidia

WORKDIR /comfyui
COPY src/extra_model_paths.yaml ./
WORKDIR /

# 6. SCRIPTS & CUSTOM NODES
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

COPY requirements.txt .
RUN uv pip install --no-cache-dir -r requirements.txt

# Install sidecar dependencies
COPY sidecar/requirements.txt /sidecar/requirements.txt
RUN uv pip install --no-cache-dir -r /sidecar/requirements.txt

ENV PIP_NO_INPUT=1
RUN comfy-node-install \
    comfyui-videohelpersuite \
    comfyui-kjnodes \
    comfyui-custom-scripts \
    comfyui-wan-vace-prep \
    comfymath \
    seedvr2_videoupscaler \
    comfyui-frame-interpolation \
    tripleksampler \
    comfyui-unload-model 

# 7. SIDECAR
COPY sidecar/handler.py /sidecar/handler.py

# 8. STARTUP SCRIPT
COPY start.sh /start.sh
RUN chmod +x /start.sh

# 9. EXPOSE PORTS (ComfyUI + sidecar)
EXPOSE 8188 8189

CMD ["/start.sh"]
