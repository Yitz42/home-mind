# The bridge is the Python standard library only: nothing to install.
FROM python:3.12-alpine
COPY bridge.py /bridge.py
ENV OPTIONS=/data/options.json PYTHONUNBUFFERED=1
CMD ["python3", "/bridge.py"]
