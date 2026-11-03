# Both targets build without network from the allowlisted context.
ARG BASE_IMAGE=python:3.11-slim-bookworm@sha256:2333bd330d12de02514770b3585cad313644316047cdee24a7acfdece6de6efb
ARG CPU_IMAGE=datamate-llm-cpu:local
FROM ${BASE_IMAGE} AS source
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATAMATE_CONTAINER=1
WORKDIR /app
COPY src/ /app/src/
COPY predict.sh /app/
RUN chmod 0555 /app/predict.sh
USER 65532:65532
ENTRYPOINT ["/app/predict.sh"]

FROM source AS llm
USER 0:0
COPY llm/python/ /usr/local/lib/python3.11/site-packages/
COPY llm/lib/ /opt/llm/lib/
COPY llm/licenses/ /opt/llm/licenses/
COPY llm/model.gguf /opt/model/model.gguf
ENV LD_LIBRARY_PATH=/opt/llm/lib
USER 65532:65532
CMD ["--data-dir", "/data", "--output-dir", "/result", "--model", "/opt/model/model.gguf"]

FROM ${CPU_IMAGE} AS cuda
USER 0:0
# Replace the binding distribution completely, so old CPU-only libraries cannot remain.
RUN rm -rf /usr/local/lib/python3.11/site-packages/llama_cpp \
    /usr/local/lib/python3.11/site-packages/llama_cpp_python-*.dist-info \
    /usr/local/lib/python3.11/site-packages/lib
COPY llm/python/ /usr/local/lib/python3.11/site-packages/
COPY llm/lib/ /opt/llm/lib/
COPY llm/licenses/ /opt/llm/licenses/
USER 65532:65532
CMD ["--data-dir", "/data", "--output-dir", "/result", "--model", "/opt/model/model.gguf", "--gpu-layers", "-1"]

# Preserve the lightweight default target used by source-only verification.
FROM source AS runtime
