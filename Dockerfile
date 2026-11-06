# CPU/CUDA stages prepare assets; every image runs the root predict.sh.
ARG BASE_IMAGE=python:3.11-slim-bookworm@sha256:2333bd330d12de02514770b3585cad313644316047cdee24a7acfdece6de6efb
ARG CPU_IMAGE=datamate-llm-cpu:local
FROM ${BASE_IMAGE} AS source
USER 0:0
# Remove legacy /app and stale source when refreshing a packaged parent.
# Keep model weights and installed runtime under /opt and /usr/local.
RUN rm -rf /app /code/src
WORKDIR /code
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATAMATE_CONTAINER=1 \
    DATAMATE_MODEL=/opt/model/model.gguf DATAMATE_GPU_LAYERS=0 DATAMATE_CACHE=/result/cache \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY src/ /code/src/
COPY --chmod=0555 predict.sh /code/predict.sh
COPY pyproject.toml uv.lock /code/
COPY docs/model-license.txt /opt/model/LICENSE
ENTRYPOINT ["bash", "/code/predict.sh"]
CMD []

FROM source AS llm
COPY llm/python/ /usr/local/lib/python3.11/site-packages/
COPY llm/lib/ /opt/llm/lib/
COPY llm/licenses/ /opt/llm/licenses/
COPY llm/model.gguf /opt/model/model.gguf
ENV LD_LIBRARY_PATH=/opt/llm/lib

FROM ${CPU_IMAGE} AS cuda
USER 0:0
RUN rm -rf /app /code/src \
    /usr/local/lib/python3.11/site-packages/llama_cpp \
    /usr/local/lib/python3.11/site-packages/llama_cpp_python-*.dist-info \
    /usr/local/lib/python3.11/site-packages/lib
COPY --from=source /code/ /code/
COPY llm/python/ /usr/local/lib/python3.11/site-packages/
COPY llm/lib/ /opt/llm/lib/
COPY llm/licenses/ /opt/llm/licenses/
WORKDIR /code
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATAMATE_CONTAINER=1 \
    DATAMATE_MODEL=/opt/model/model.gguf DATAMATE_GPU_LAYERS=-1 DATAMATE_CACHE=/result/cache \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 LD_LIBRARY_PATH=/opt/llm/lib
ENTRYPOINT ["bash", "/code/predict.sh"]
CMD []

# Final source refresh from a local parent already containing model/runtime assets.
# Generated metadata adds no second prediction script.
FROM source AS submission
ARG SUBMISSION_MODEL=/opt/model/model.gguf
ENV DATAMATE_MODEL=${SUBMISSION_MODEL} DATAMATE_GPU_LAYERS=-1
COPY submission/ /code/
