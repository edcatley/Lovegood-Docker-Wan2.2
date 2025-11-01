# Step 1: Start from a standard RunPod base image with PyTorch and CUDA pre-installed.
# The "-devel" tag includes tools like git, which we need.
FROM runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel

# Step 2: Install essential system-level packages.
# git is for cloning repos, aria2c is a much faster multi-connection downloader.
RUN apt-get update && apt-get install -y git aria2c && rm -rf /var/lib/apt/lists/*

# Step 3: Set up the main working directory for our application.
WORKDIR /app

# Step 4: Install ComfyUI from its official repository.
RUN git clone https://github.com/comfyanonymous/ComfyUI.git

# Step 5: Install ComfyUI's own Python dependencies.
# This keeps its environment separate and clean.
RUN pip install --no-cache-dir -r ComfyUI/requirements.txt

# Step 6: [CRITICAL] Install any ComfyUI Custom Nodes your workflow requires.
# This makes your build reproducible. For every node you use, add a line here.
# --- EXAMPLE SECTION ---
# RUN cd ComfyUI/custom_nodes && git clone https://github.com/ltdrdata/ComfyUI-Manager.git
# RUN cd ComfyUI/custom_nodes && git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git
# # Some custom nodes have their own dependencies, so install them too.
# RUN pip install --no-cache-dir -r ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt
# --- END EXAMPLE SECTION ---


# Step 7: Install the Python packages for our own handler script.
# We copy this file first to take advantage of Docker's layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 8: Copy our application code and startup script into the container.
COPY src/ /app/src/
COPY start.sh .

# Step 9: Make our startup script executable.
RUN chmod +x start.sh

# Step 10: Set the default command to run when the container starts.
# This will execute our script, which will then launch the necessary services.
CMD ["./start.sh"]