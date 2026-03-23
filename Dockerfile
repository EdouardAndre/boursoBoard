FROM debian:12-slim

COPY --from=docker.io/astral/uv:latest /uv /bin/

WORKDIR /workspace

COPY ./pyproject.toml ./uv.lock /workspace/
RUN uv sync --locked

COPY ./bourse /workspace/bourse/

CMD ["uv", "run", "bourse/app.py"]
