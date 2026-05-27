# SEED Backend

Serverless backend for SEED — a daily thought-journaling service. Thoughts are
embedded, matched against a shared index of canonical thoughts (vector kNN), and
either linked to an existing canonical or used to mint a new one via an LLM.

Architecture and rationale: see [`../SEED_Backend_Technical_Design.md`](../SEED_Backend_Technical_Design.md).
This is the **P0 skeleton** — infrastructure and application code are in place;
some operational steps (model export, kill-switch Lambda) are noted below as
follow-ups.

## Layout

```
seed-backend/
├── Dockerfile            # Lambda container (python:3.12-arm64)
├── pytest.ini
├── infra/                # AWS CDK v2 (TypeScript)
│   ├── bin/app.ts
│   ├── lib/seed-api-stack.ts
│   ├── lib/security-stack.ts
│   ├── lib/pinecone-custom-resource.ts
│   └── lambda/pinecone-cr/index.py   # custom-resource handler
└── src/                  # Lambda application (Python 3.12)
    ├── handler.py        # Powertools REST router + POST /thoughts flow
    ├── config.py · models.py · matching.py
    ├── embedding.py · canonicalization.py
    ├── db.py · pinecone_client.py
    └── tests/
```

The GitHub Actions workflow lives at the **repo root** (`../.github/workflows/deploy.yml`)
because Actions only runs workflows from the root; its steps `cd` into this folder.

## Prerequisites (one-time, before first deploy)

1. **Secrets Manager** — create the two secrets the stack references (per stage):
   ```bash
   aws secretsmanager create-secret --name seed/pinecone-api-key-dev   --secret-string '<PINECONE_API_KEY>'
   aws secretsmanager create-secret --name seed/anthropic-api-key-dev  --secret-string '<ANTHROPIC_API_KEY>'
   ```
2. **CDK bootstrap**:
   ```bash
   cd infra && npm install && npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
   ```
3. **Model weights** — export MiniLM-L6-v2 to ONNX and upload to the model
   bucket created by the stack (`seed-models-<stage>`) at key `minilm-l6-v2.onnx`.

## Deploy

```bash
cd infra
npx cdk diff   -c env=dev -c alertEmail=you@example.com
npx cdk deploy --all -c env=dev -c alertEmail=you@example.com
npx cdk destroy --all -c env=dev          # dev teardown
```

The Lambda is a container image. CI builds it from `seed-backend/` and pushes to
ECR before `cdk deploy`; for a manual first deploy, build/push the image to the
`seed-lambda-<stage>` repo and pass `IMAGE_TAG`.

## Local checks

```bash
# infra synthesizes
cd infra && npm install && npx cdk synth -c env=dev -c alertEmail=test@example.com

# python modules import (heavy deps are lazy, so no creds/ML libs needed)
cd ../src && python -c "import handler, embedding, canonicalization, models, matching, db, pinecone_client, config"

# unit tests (pure logic)
cd .. && pytest -q
```

## Endpoints

| Method | Path | Auth |
|---|---|---|
| POST | `/auth/{signup,login,refresh}` | none |
| POST | `/thoughts` | JWT |
| POST | `/thoughts/confirm` | JWT |
| GET | `/thoughts/mine?start=&end=` | JWT |
| GET | `/thoughts/today` | JWT |
| GET | `/rooms/{canonical_id}` | JWT |
| GET | `/rooms/{canonical_id}/thoughts?limit=&cursor=` | JWT |

## Follow-ups (not in this skeleton)

- **Kill-switch Lambda** — the $25 budget notifies the SNS topic; wiring a Lambda
  that disables the API Gateway stage is a `TODO` in `security-stack.ts` (doc §7.5).
- **MiniLM ONNX export + S3 upload** — operational step above.
- **Email-verification gating** before first thought submission (doc §7.3).
- **Room privacy** — `/rooms/{id}/thoughts` currently returns only non-identifying
  fields (no raw text), matching the P0 decision in doc §10.
