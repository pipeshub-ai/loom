FROM python:3.12-slim AS base
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir .

FROM base AS with-mongo
RUN pip install --no-cache-dir ".[mongo]"

FROM base AS with-postgres
RUN pip install --no-cache-dir ".[postgres]"

FROM base
COPY examples/ examples/
CMD ["python", "-m", "workflow_builder"]
