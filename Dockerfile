# Cold Storage — pure Python 3 standard library + SQLite, nothing to pip install.
FROM python:3.12-slim

WORKDIR /app

# Only the application code. The database is NOT baked into the image — it lives
# on a persistent volume mounted at /data (see fly.toml), and .dockerignore keeps
# local *.db files (real lab data) out of the build.
COPY app.py db.py ./
COPY static ./static

# Runtime config comes from fly.toml [env]; these are fallbacks.
ENV PORT=8080 \
    COLDSTORAGE_DB=/data/coldstorage.db

EXPOSE 8080
CMD ["python3", "app.py"]
