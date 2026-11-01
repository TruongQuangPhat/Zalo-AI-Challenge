# CPU-only Phase 0 stub; not a Blackwell/model-serving submission image.
# Resolve BASE_IMAGE to an approved digest before accepting a final build.
ARG BASE_IMAGE=python:3.11-slim-bookworm
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATAMATE_CONTAINER=1
WORKDIR /app
COPY src/ /app/src/
COPY predict.sh /app/
RUN chmod 0555 /app/predict.sh
USER 65532:65532
ENTRYPOINT ["/app/predict.sh"]
