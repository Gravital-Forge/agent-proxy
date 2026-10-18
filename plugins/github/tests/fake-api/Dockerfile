FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache python3 openssl
COPY fakeapi.py /fakeapi.py
RUN mkdir /pki \
    && cd /pki \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 1 -keyout ca.key -out ca.crt -subj "/CN=fake-github-test-ca" -addext keyUsage=critical,keyCertSign,cRLSign \
    && openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=api.github.com" \
    && printf 'subjectAltName=DNS:api.github.com,DNS:uploads.github.com\n' > san.ext \
    && openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 1 -extfile san.ext -out server.crt \
    && chmod 644 /pki/*
CMD ["python3", "-u", "/fakeapi.py"]
