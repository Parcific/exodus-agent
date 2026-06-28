FROM python:3.13-slim

# Non-root user so the container doesn't run as root
RUN useradd --create-home --shell /bin/bash exodus
WORKDIR /home/exodus/app

# Copy package files first so pip install layer is cached
COPY pyproject.toml ./
COPY exodus_agent/ ./exodus_agent/

RUN pip install --no-cache-dir -e .

# Workspace is mounted at runtime — keep it outside the image
WORKDIR /workspace
USER exodus

ENTRYPOINT ["exodus"]
CMD ["--help"]
