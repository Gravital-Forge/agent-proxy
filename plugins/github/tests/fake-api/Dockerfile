FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache python3 openssl
COPY fakeapi.py /fakeapi.py
RUN mkdir /pki \
    && cd /pki \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 1 -keyout ca.key -out ca.crt -subj "/CN=fake-github-test-ca" \
    && openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=api.github.com" \
    && printf 'subjectAltName=DNS:api.github.com\n' > san.ext \
    && openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 1 -extfile san.ext -out server.crt \
    && chmod 644 /pki/*
CMD ["python3", "-u", "/fakeapi.py"]
