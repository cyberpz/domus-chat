FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir edge-tts
COPY app.py index.html ./
COPY static ./static
ENV PORT=8080 DB_PATH=/data/chat.db
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4); sys.exit(0)" || exit 1
CMD ["python", "-u", "app.py"]
