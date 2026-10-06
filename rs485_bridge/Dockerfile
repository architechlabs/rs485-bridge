FROM python:3.13-slim-bookworm
ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version=$BUILD_VERSION io.hass.type="app" io.hass.arch=$BUILD_ARCH
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/bridge/app:/opt/bridge/engine
WORKDIR /opt/bridge
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY engine ./engine
COPY profiles ./profiles
COPY web ./web
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=3)"
CMD ["python", "-m", "rs485_bridge", "--data", "/data", "--host", "0.0.0.0", "--ingress"]
