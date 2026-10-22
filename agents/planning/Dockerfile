# 빌드 컨텍스트는 레포 루트: docker build -f agents/planning/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --no-editable --package ax-agent-planning

EXPOSE 8101
CMD ["uvicorn", "planning_agent.main:app", "--host", "0.0.0.0", "--port", "8101"]
