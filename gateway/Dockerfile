# qq-fish 预算网关镜像
# 复用本地已有的 MaiBot 镜像（自带 python3.13 + fastapi + uvicorn + httpx），无需拉取基础镜像
FROM sengokucola/maibot:latest

WORKDIR /app
COPY app.py /app/app.py
COPY settings.json /app/settings.json

# 覆盖镜像原有入口，只跑网关
ENTRYPOINT []
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8009", "--log-level", "info"]

EXPOSE 8009
