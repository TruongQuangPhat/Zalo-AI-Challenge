# Single-stage image following the DataMate Docker guide §9 (RTX 5090 / CUDA 12.8 base).
FROM vllm/vllm-openai:v0.10.1.1
ENTRYPOINT []
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1 \
    PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /code
COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt
# One model on one GPU (tensor_parallel_size=1): no NCCL multi-GPU workaround needed.
COPY models/ /code/models/
COPY training_code/ /code/training_code/
COPY predict.sh README.md /code/
COPY src/ /code/src/
RUN chmod +x /code/predict.sh
CMD ["bash", "/code/predict.sh"]
