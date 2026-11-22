ARG INSTANCE_IMAGE
FROM ${INSTANCE_IMAGE}
COPY apx_test_labels.pth apx_test_labels.py /app/.venv/lib/python3.12/site-packages/
ENV AGENT_PROXY_VERIFIER_IMAGE=apx-verifier:it
