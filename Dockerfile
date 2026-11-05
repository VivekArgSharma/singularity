# Fetch robot: Panda3D engine + agents + console in one container.
# Rendering runs on the CPU (Mesa llvmpipe) inside a virtual X display, so no GPU is needed.
# Only the console is published ($PORT, default 7860); the agent API stays on 127.0.0.1:8080.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xauth libgl1 libgl1-mesa-dri libglu1-mesa libegl1 libgles2 \
        libx11-6 libxext6 libxrender1 libxrandr2 libxcursor1 libxi6 libxinerama1 libxxf86dga1 libxxf86vm1 \
        libglib2.0-0 libsm6 libice6 fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs containers as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 \
    PORT=7860 CONSOLE_BIND=0.0.0.0 USE_CLAUDE_API=false FETCH_LOWMEM=1 \
    LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe LP_NUM_THREADS=2 MALLOC_ARENA_MAX=2
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .
RUN mkdir -p outputs/runs clips && chmod +x deploy/start.sh

EXPOSE 7860
CMD ["deploy/start.sh"]
