FROM registry.access.redhat.com/ubi9/python-312:9.5
COPY --from=ghcr.io/astral-sh/uv:0.6.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
USER root
RUN dnf install -y git-lfs mesa-libGL && \
    dnf clean all && \
    git lfs install
WORKDIR /app
COPY uv.lock pyproject.toml /app/
RUN uv sync --frozen --no-install-project --no-dev
COPY /src /app
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["bash", "-c", "source /app/.venv/bin/activate && exec python /app/model.py"]
