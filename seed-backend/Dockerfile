# SEED API Lambda container image (ARM64/Graviton, Python 3.12).
# Model weights are NOT baked in — downloaded from S3 on cold start (doc §4.2).
FROM public.ecr.aws/lambda/python:3.12-arm64

COPY src/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
