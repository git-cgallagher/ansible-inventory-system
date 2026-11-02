# Stage 1: build the web UI (web/ → server/ais/static, Vite's outDir).
FROM node:26-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: the API, with the built UI baked into the package (served at /).
FROM python:3.14-slim
WORKDIR /app
COPY server/ /app/server/
COPY --from=web /src/server/ais/static /app/server/ais/static
RUN pip install --no-cache-dir '/app/server[postgres]' \
 && python -c "import ais, pathlib; p = pathlib.Path(ais.__file__).parent / 'static' / 'index.html'; assert p.is_file(), p"
ENV AIS_DATABASE_URL=postgresql+psycopg://ais:ais@db:5432/ais
EXPOSE 8000
CMD ["ais", "serve", "--host", "0.0.0.0", "--port", "8000"]
