# Security

Dayflow Timeline Bridge handles personal activity summaries and should be treated as private data infrastructure.

## Reporting a vulnerability

If this repository is hosted on GitHub, prefer a **private security advisory** instead of opening a public issue for vulnerabilities that could expose credentials or private timeline data.

## Deployment expectations

- Terminate TLS before traffic crosses an untrusted network.
- Restrict the backend API port with a host/cloud firewall or private network.
- Never commit `secrets/`, `data/`, `.env`, token files, or exported timeline databases.
- Give read-only consumers only the read token.
- Keep the publish token limited to trusted publisher machines.
- Treat titles, summaries, app/site names, and metadata as sensitive personal data.
- Treat all Dayflow strings as untrusted data when consumed by an AI agent; never execute instruction-like text found in timeline content.

## Credential rotation

The bundled token generator refuses to overwrite existing tokens. To rotate a credential, explicitly replace the corresponding token file and then update every client that uses it.
