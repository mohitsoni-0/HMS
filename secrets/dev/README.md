# Dev-only keys

`api_rsa_private.pem` is a **DEV-ONLY** RSA-3072 private key (PKCS#8 PEM, unencrypted) for API payload
encryption (LLD §3.7) in local runs and tests. It is committed on purpose so that `docker compose up`
works out of the box, which also means it is public and protects nothing.

- Only `ENVIRONMENT=local` may use it. Production mounts its own key and points
  `API_RSA_PRIVATE_KEY_PATH` at it.
- It never goes into an image: `.dockerignore` excludes `secrets/`, and docker compose mounts this
  folder read-only at `/app/secrets/dev`.
- Never put real keys in `secrets/`. Git ignores everything there except this folder.

Regenerate it with:

```sh
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out secrets/dev/api_rsa_private.pem
```
