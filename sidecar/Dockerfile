FROM python:3.13-slim
WORKDIR /srv
# requirements.txt 从仓库根的 uv.lock 导出（带 hash），改了依赖要重新导出：
#   uv export --frozen --only-group sidecar --no-emit-project -o sidecar/requirements.txt
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /srv/requirements.txt
COPY app /srv/app
EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
