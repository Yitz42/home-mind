ARG BUILD_FROM
FROM python:3.13-alpine
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# tzdata: without it TZ is ignored and every "today" is UTC
RUN apk add --no-cache tzdata
RUN pip install --no-cache-dir "aiohttp>=3.10,<4" "metaphone==0.6" "libzim>=3.4,<5" "numpy>=1.26,<3" "cryptography>=42" "pypdf>=4"
COPY rootfs /
WORKDIR /app
CMD ["python", "-m", "homemind"]
