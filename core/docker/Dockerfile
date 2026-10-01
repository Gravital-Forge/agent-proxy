FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
RUN pip install --no-cache-dir uv==0.11.24
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY agent_proxy ./agent_proxy
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    DOCKER_HOST=unix:///var/run/docker.sock \
    AGENT_PROXY_CONFIG=/etc/agent-proxy/config.yaml
ENTRYPOINT ["agent-proxy"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
