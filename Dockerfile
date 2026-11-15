# Single-stage image following the DataMate Docker guide §9 reference Dockerfile
# (vLLM base with RTX 5090 / CUDA 12.8 support; Qwen3-14B split over two GPUs).
FROM vllm/vllm-openai:v0.10.1.1
ENTRYPOINT []
# Two-GPU stack from the guide: the tested pair lacked P2P access, and NCCL 2.26.2 hung at
# two-GPU initialization (fixed by nvidia-nccl-cu12==2.27.7).
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1 \
    PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NCCL_P2P_DISABLE=1
WORKDIR /code
COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt
RUN pip install --no-cache-dir --no-deps nvidia-nccl-cu12==2.27.7
COPY models/ /code/models/
COPY training_code/ /code/training_code/
COPY predict.sh README.md /code/
COPY src/ /code/src/
RUN chmod +x /code/predict.sh
CMD ["bash", "/code/predict.sh"]
