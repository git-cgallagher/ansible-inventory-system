FROM python:3.12-slim
WORKDIR /app
COPY server/ /app/server/
RUN pip install --no-cache-dir '/app/server[postgres]'
ENV AIS_DATABASE_URL=postgresql+psycopg://ais:ais@db:5432/ais
EXPOSE 8000
CMD ["ais", "serve", "--host", "0.0.0.0", "--port", "8000"]
