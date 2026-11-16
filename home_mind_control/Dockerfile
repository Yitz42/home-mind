# llama.cpp's own server image (amd64 and arm64). Home Mind reaches it on the internal network only: no port is
# published on the host.
FROM ghcr.io/ggml-org/llama.cpp:server
COPY run.sh /run.sh
RUN chmod a+x /run.sh
ENTRYPOINT ["/run.sh"]
