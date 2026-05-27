# SEED

**Thought Journaling Service — Backend Technical Design Document**

P0 Scope — Serverless Architecture
DynamoDB + Pinecone Serverless + Lambda

May 2026 — v1.0 | CONFIDENTIAL

---

## 1. System Overview

SEED is a daily thought journaling service where users submit a single tweet-sized thought (max 280 characters) per day. Thoughts are semantically matched against a shared index of canonical thoughts using bi-encoder vector retrieval. When a user's thought closely matches an existing canonical, it is linked to that canonical node (a "room"). When no match exists, the system creates a new canonical by generalizing the user's input via a small LLM call.

The P0 backend is fully serverless, optimized for near-zero baseline cost at low traffic with linear cost scaling. There are no always-on compute or search instances.

### 1.1 P0 Feature Set

- User authentication (signup, login, email verification)
- Submit one thought per day (≤280 characters, enforced at DB level)
- On submission: embed → kNN search → threshold match → link to existing canonical OR create new canonical
- View personal thought history (today, last week, last month)
- View which canonical a thought linked to (the "room")
- Browse a room: generalized canonical text + count of linked thoughts

### 1.2 Architecture Diagram

```
Client (React Native / Next.js)
    │
    ▼
API Gateway (REST, usage plan + API key + throttling)
    │
    ▼
Lambda (Python 3.12, ARM64/Graviton)
    ├── POST /thoughts       → embed + search + link/create
    ├── GET  /thoughts/mine  → user's history
    ├── GET  /rooms/{id}     → canonical detail
    └── POST /auth/*         → Cognito wrapper
    │
    ├──► Pinecone Serverless (vector search, canonical index)
    ├──► DynamoDB (user_thoughts, users, canonicals)
    ├──► S3 (model artifacts: MiniLM weights)
    └──► Anthropic API (canonicalization, on-demand)
```

| Component | Service | Purpose |
|---|---|---|
| API Layer | API Gateway (REST) | Routing, throttling, usage plans, WAF attachment |
| Compute | Lambda (Python 3.12, ARM64/Graviton) | All business logic, embedding inference |
| Auth | Amazon Cognito | User pool, JWT issuance, email verification |
| Primary Store | DynamoDB (on-demand) | Users, thoughts, canonical metadata |
| Vector Index | Pinecone Serverless | kNN retrieval over canonical thought embeddings |
| Model Weights | S3 | ONNX-exported MiniLM-L6-v2 weights (~90MB) |
| Canonicalization | Anthropic API (Claude Haiku) | Generalize user thoughts into canonical text |

---

## 2. Data Model — DynamoDB

DynamoDB is the system of record for all structured data. The design uses a single-table pattern with three entity types: Users, Thoughts, and Canonicals. On-demand capacity mode eliminates provisioned throughput management and charges only for consumed read/write units.

### 2.1 Table: seed-primary

Single-table design with composite keys. All access patterns are satisfied by the base table plus one GSI.

**Entity: User**

| Attribute | Type | Description |
|---|---|---|
| PK | S | `USER#{user_id}` — UUID v4 |
| SK | S | `PROFILE` |
| email | S | User email (unique, enforced by Cognito) |
| display_name | S | Optional display name |
| thought_count | N | Running total of submitted thoughts |
| last_thought_date | S | ISO date of most recent thought (YYYY-MM-DD) |
| created_at | S | ISO 8601 timestamp |
| updated_at | S | ISO 8601 timestamp |

**Entity: Thought**

| Attribute | Type | Description |
|---|---|---|
| PK | S | `USER#{user_id}` |
| SK | S | `THOUGHT#{YYYY-MM-DD}` — enforces one per day |
| raw_text | S | Original user input (≤280 chars) |
| canonical_id | S | FK to linked canonical thought UUID |
| similarity_score | N | Cosine similarity to canonical at time of linking |
| match_type | S | `AUTO_LINKED` \| `USER_CONFIRMED` \| `NEW_CANONICAL` |
| created_at | S | ISO 8601 timestamp |
| GSI1PK | S | `CANONICAL#{canonical_id}` — for room queries |
| GSI1SK | S | `THOUGHT#{YYYY-MM-DD}` |

**Entity: Canonical**

| Attribute | Type | Description |
|---|---|---|
| PK | S | `CANONICAL#{canonical_id}` — UUID v4 |
| SK | S | `META` |
| text | S | Generalized canonical thought text (≤200 chars) |
| linked_count | N | Atomic counter of linked thoughts |
| category_tags | L | List of category strings (future use) |
| source_thought_id | S | PK+SK of the thought that created this canonical |
| created_at | S | ISO 8601 timestamp |
| updated_at | S | ISO 8601 timestamp |

### 2.2 Global Secondary Index

| GSI Name | Partition Key | Sort Key | Purpose |
|---|---|---|---|
| GSI1 | GSI1PK (`CANONICAL#{id}`) | GSI1SK (`THOUGHT#{date}`) | Query all thoughts linked to a canonical (room view), sorted by date |

### 2.3 Access Patterns

| Access Pattern | Key Condition | Index |
|---|---|---|
| Get user profile | PK = `USER#{id}`, SK = `PROFILE` | Base |
| Get user's thought for a specific date | PK = `USER#{id}`, SK = `THOUGHT#{date}` | Base |
| Get user's thought history (date range) | PK = `USER#{id}`, SK BETWEEN `THOUGHT#{start}` AND `THOUGHT#{end}` | Base |
| Get canonical metadata | PK = `CANONICAL#{id}`, SK = `META` | Base |
| Get all thoughts in a room (room view) | GSI1PK = `CANONICAL#{id}` | GSI1 |
| Count thoughts in a room | GSI1PK = `CANONICAL#{id}` (SELECT COUNT) | GSI1 |

### 2.4 Write Operations

**Submit Thought (Conditional Write)**

The one-thought-per-day constraint is enforced at the database level using a conditional expression. This prevents duplicates even under race conditions or replay attacks.

```python
table.put_item(
    Item={
        'PK': f'USER#{user_id}',
        'SK': f'THOUGHT#{today_iso}',
        'raw_text': sanitized_text,
        'canonical_id': matched_canonical_id,
        'similarity_score': Decimal(str(score)),
        'match_type': 'AUTO_LINKED',
        'created_at': now_iso,
        'GSI1PK': f'CANONICAL#{matched_canonical_id}',
        'GSI1SK': f'THOUGHT#{today_iso}',
    },
    ConditionExpression='attribute_not_exists(SK)'
)
```

If SK already exists for today, DynamoDB raises `ConditionalCheckFailedException`. The Lambda catches this and returns HTTP 409 Conflict.

**Increment Canonical Link Count (Atomic Update)**

```python
table.update_item(
    Key={'PK': f'CANONICAL#{canonical_id}', 'SK': 'META'},
    UpdateExpression='SET linked_count = linked_count + :inc, updated_at = :now',
    ExpressionAttributeValues={':inc': 1, ':now': now_iso}
)
```

### 2.5 Capacity and Cost Estimation

DynamoDB on-demand pricing (us-east-1): $1.25 per million write request units, $0.25 per million read request units. Storage: $0.25/GB/month.

| Scale (DAU) | Writes/Month | Reads/Month | Storage | Est. Cost/Month |
|---|---|---|---|---|
| 100 | ~6,000 | ~30,000 | <1 MB | $0.02 |
| 1,000 | ~60,000 | ~300,000 | <10 MB | $0.15 |
| 10,000 | ~600,000 | ~3,000,000 | <100 MB | $1.50 |
| 100,000 | ~6,000,000 | ~30,000,000 | <1 GB | $15.00 |

Reads assume 5x multiplier over writes to account for history queries, room views, and profile fetches. At 100K DAU, DynamoDB is still under $15/month — it will never be your bottleneck.

---

## 3. Vector Index — Pinecone Serverless

Pinecone Serverless serves as the ANN (Approximate Nearest Neighbor) index for canonical thought embeddings. It replaces OpenSearch, which has a $700+/month minimum on AWS. Pinecone's serverless tier provides true pay-per-use kNN search with a generous free tier.

### 3.1 Index Configuration

| Parameter | Value | Rationale |
|---|---|---|
| Index Name | `seed-canonicals` | Single index for all canonical thoughts |
| Dimension | 384 | Matches MiniLM-L6-v2 / bge-small-en output |
| Metric | cosine | Standard for semantic similarity on normalized embeddings |
| Cloud/Region | aws / us-east-1 | Co-located with Lambda for lowest latency |
| Pod Type | Serverless (s1) | No minimum cost, scales to zero |

### 3.2 Document Schema

Each vector in the Pinecone index represents a single canonical thought. Metadata fields enable filtered queries and provide context without a DynamoDB round-trip.

```json
{
  "id": "canonical_uuid",
  "values": [0.0123, -0.0456, ...],
  "metadata": {
    "text": "Generalized canonical thought text",
    "linked_count": 42,
    "category": "existential",
    "created_at": "2026-05-26T00:00:00Z"
  }
}
```

### 3.3 Operations

**Query (on every thought submission)**

When a user submits a thought, the Lambda embeds it with MiniLM and queries Pinecone for the top-5 nearest canonical thoughts.

```python
from pinecone import Pinecone

pc = Pinecone(api_key=os.environ['PINECONE_API_KEY'])
index = pc.Index('seed-canonicals')

results = index.query(
    vector=user_thought_embedding,  # list[float], len=384
    top_k=5,
    include_metadata=True
)

# results.matches = [
#   { 'id': 'uuid', 'score': 0.92, 'metadata': {...} },
#   ...
# ]
```

**Upsert (on new canonical creation)**

When no existing canonical matches above the threshold, the system creates a new canonical and inserts its embedding into Pinecone.

```python
index.upsert(
    vectors=[{
        'id': new_canonical_id,
        'values': generalized_embedding,
        'metadata': {
            'text': generalized_text,
            'linked_count': 1,
            'category': inferred_category,
            'created_at': now_iso
        }
    }]
)
```

**Metadata Update (on link count change)**

Pinecone supports partial metadata updates without re-indexing the vector. When a thought links to an existing canonical, update the count.

```python
index.update(
    id=canonical_id,
    set_metadata={'linked_count': new_count}
)
```

### 3.4 Matching Logic

The matching decision uses a two-threshold system to minimize both false positives and unnecessary LLM calls.

| Similarity Score | Action | match_type Value |
|---|---|---|
| ≥ 0.85 | Auto-link to top canonical. No user intervention. | `AUTO_LINKED` |
| 0.60 – 0.84 | Present top-3 candidates to user: "Did you mean?" | `USER_CONFIRMED` |
| < 0.60 | No match. Create new canonical via LLM generalization. | `NEW_CANONICAL` |

Thresholds are tunable. Start with these values and adjust based on user feedback during early access. Track the distribution of similarity scores to identify the optimal boundaries. The grey zone (0.60–0.84) doubles as a labeling mechanism: user confirmations generate ground-truth similarity pairs for future threshold calibration or classifier training.

### 3.5 Pinecone Cost Estimation

Pinecone Serverless pricing: free tier includes 2GB storage and 100K vectors in 384 dimensions. Beyond free tier: ~$0.04 per 100K read units, $2 per 1M write units.

| Scale (DAU) | Queries/Month | Upserts/Month | Storage | Est. Cost/Month |
|---|---|---|---|---|
| 100 | ~3,000 | ~300 | <1 MB | $0 (free tier) |
| 1,000 | ~30,000 | ~1,500 | <5 MB | $0 (free tier) |
| 10,000 | ~300,000 | ~5,000 | <50 MB | $0–$2 |
| 100,000 | ~3,000,000 | ~15,000 | <500 MB | $5–$15 |

Upsert volume is sublinear relative to DAU because new canonical creation rate decreases as the index matures. At 50K+ canonicals, most thoughts match existing entries. This is the key cost advantage of the architecture.

---

## 4. Embedding Pipeline

The bi-encoder model runs on Lambda using ONNX Runtime for CPU inference. No GPU required. The model is small enough (90MB) to load from S3 into Lambda's `/tmp` on cold start, then stays in memory for warm invocations.

### 4.1 Model Selection

| Model | Params | Dim | Size (ONNX) | Inference (280 chars) | Quality (STS-B) |
|---|---|---|---|---|---|
| all-MiniLM-L6-v2 (recommended) | 22M | 384 | ~90MB | 15–30ms on ARM64 | 0.843 |
| bge-small-en-v1.5 | 33M | 384 | ~130MB | 25–45ms on ARM64 | 0.851 |
| e5-small-v2 | 33M | 384 | ~130MB | 25–45ms on ARM64 | 0.848 |

MiniLM-L6-v2 is the recommended starting model. It is the smallest, fastest, and produces high-quality embeddings for short text. The quality delta versus bge-small is marginal for texts under 280 characters.

### 4.2 Lambda Deployment

- Package `onnxruntime` and tokenizer as a Lambda layer or Docker container image
- Store ONNX model weights in S3 (`s3://seed-models/minilm-l6-v2.onnx`)
- On cold start: download from S3 to `/tmp/model.onnx`, load ONNX session
- On warm invocations: model already resident in memory, inference only
- Lambda config: 1024MB memory, 30s timeout, ARM64 (Graviton2)

**Cold Start Breakdown**

| Phase | Duration |
|---|---|
| Lambda init (Python runtime) | ~300ms |
| S3 download (90MB model) | ~800ms–1.2s |
| ONNX session load | ~500ms |
| Total cold start | ~1.5–2.0s |
| Warm inference (280 chars) | ~15–30ms |

Cold starts are acceptable for P0. If they become a UX issue, add 1 provisioned concurrency ($15/month) to keep one instance warm. At P0 scale, most invocations within a usage session will hit warm instances due to Lambda's reuse behavior.

### 4.3 Embedding Code

```python
import onnxruntime as ort
import numpy as np
from transformers import AutoTokenizer

# Global scope (persists across warm invocations)
tokenizer = None
session = None

def load_model():
    global tokenizer, session
    if session is not None:
        return
    import boto3
    s3 = boto3.client('s3')
    s3.download_file('seed-models', 'minilm-l6-v2.onnx', '/tmp/model.onnx')
    session = ort.InferenceSession('/tmp/model.onnx')
    tokenizer = AutoTokenizer.from_pretrained(
        'sentence-transformers/all-MiniLM-L6-v2'
    )

def embed(text: str) -> list[float]:
    load_model()
    encoded = tokenizer(
        text, padding=True, truncation=True,
        max_length=128, return_tensors='np'
    )
    outputs = session.run(
        None,
        {
            'input_ids': encoded['input_ids'],
            'attention_mask': encoded['attention_mask'],
            'token_type_ids': encoded['token_type_ids']
        }
    )
    # Mean pooling over token embeddings
    token_embeddings = outputs[0]
    mask = encoded['attention_mask']
    expanded_mask = np.expand_dims(mask, -1)
    summed = np.sum(token_embeddings * expanded_mask, axis=1)
    counted = np.clip(mask.sum(axis=1, keepdims=True), 1, None)
    embedding = (summed / counted)[0]
    # L2 normalize
    norm = np.linalg.norm(embedding)
    return (embedding / norm).tolist()
```

---

## 5. Canonicalization via LLM

When a user's thought does not match any existing canonical (similarity < 0.60), the system creates a new canonical by generalizing the user's input. This ensures canonical thoughts are person-agnostic and semantically general enough to attract future matches.

### 5.1 LLM Choice

Anthropic Claude Haiku via API. Chosen for low latency (<500ms), low cost (~$0.25/M input tokens, ~$1.25/M output tokens), and sufficient quality for short-text summarization. Total cost per canonicalization call: ~$0.0001 (100 input tokens + 50 output tokens).

### 5.2 Prompt Template

```
SYSTEM: You generalize personal thoughts into universal statements.
Remove personal details, names, specific dates, and identifying info.
Output a single thought under 200 characters. No quotes, no preamble.

USER:
<user_thought>
{raw_user_text}
</user_thought>

Generalize this into a universal thought that captures the core meaning.
```

**Examples**

| User Input | Generalized Canonical |
|---|---|
| My boss Sarah totally ignored my presentation today and it makes me feel invisible | Being overlooked at work creates a deep sense of invisibility |
| I wonder if I should move to NYC or stay in SF, both have tradeoffs | The tension of choosing between two places that each offer something important |
| Just had the best ramen of my life in Hokkaido | Discovering an extraordinary version of a beloved food while traveling |
| Sometimes I think I have too many acquaintances and not enough real friends | The realization that social breadth may come at the cost of relational depth |

### 5.3 Post-Processing Pipeline

After the LLM generates a canonical, the system embeds the generalized text (not the original user text) and upserts it into Pinecone. This ensures the index contains embeddings of generalized thoughts, which increases match rates for future users.

1. Call Haiku with user's raw text → receive generalized text
2. Validate output is ≤200 chars, non-empty, no PII detected
3. Embed the generalized text with MiniLM
4. Generate UUID for new canonical
5. Upsert vector + metadata into Pinecone
6. Write canonical record to DynamoDB
7. Write user's thought record to DynamoDB (linked to new canonical)

Steps 5–7 are not transactional across services. If Pinecone upsert succeeds but DynamoDB write fails, the vector exists without a metadata record. The system tolerates this: orphaned vectors are inert (they'll match future queries but point to a nonexistent canonical, which the Lambda handles with a fallback). A nightly reconciliation job can clean orphans if needed.

---

## 6. API Design

REST API exposed through API Gateway. All endpoints except `/auth/*` require a valid Cognito JWT in the Authorization header.

### 6.1 Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/signup` | None | Register new user via Cognito |
| POST | `/auth/login` | None | Authenticate, receive JWT |
| POST | `/auth/refresh` | Refresh token | Refresh access token |
| POST | `/thoughts` | JWT | Submit today's thought |
| GET | `/thoughts/mine?start=&end=` | JWT | User's thought history by date range |
| GET | `/thoughts/today` | JWT | Get today's thought (or 404) |
| GET | `/rooms/{canonical_id}` | JWT | Canonical detail + linked thought count |
| GET | `/rooms/{canonical_id}/thoughts?limit=&cursor=` | JWT | Paginated thoughts in a room |

### 6.2 POST /thoughts — Core Flow

This is the most complex endpoint. The full request lifecycle:

1. Validate JWT, extract user_id from claims
2. Validate input: Pydantic model, ≤280 chars, sanitize control characters
3. Check if thought already exists for today (DynamoDB conditional write will enforce, but early check avoids unnecessary embedding)
4. Embed user's raw text with MiniLM (15–30ms)
5. Query Pinecone top-5 nearest canonicals (~50–100ms)
6. Apply threshold logic:
   - **Score ≥ 0.85:** Auto-link. Write thought to DDB, increment canonical count, return 201.
   - **Score 0.60–0.84:** Return 200 with candidates array. Client shows picker. User selection triggers a follow-up `POST /thoughts/confirm`.
   - **Score < 0.60:** Call Haiku for generalization (~300–500ms). Create new canonical in Pinecone + DDB. Link thought. Return 201.

**Response Schema (201 Created)**

```json
{
  "thought_id": "USER#abc123|THOUGHT#2026-05-26",
  "canonical_id": "uuid",
  "canonical_text": "The generalized thought...",
  "similarity_score": 0.91,
  "match_type": "AUTO_LINKED"
}
```

**Response Schema (200 — Candidates)**

```json
{
  "status": "candidates",
  "candidates": [
    { "canonical_id": "uuid", "text": "...", "score": 0.78 },
    { "canonical_id": "uuid", "text": "...", "score": 0.72 },
    { "canonical_id": "uuid", "text": "...", "score": 0.65 }
  ],
  "raw_text_hash": "sha256_of_input"
}
```

---

## 7. Security & Rate Limiting

Defense-in-depth with five layers. The design principle: every layer that rejects a request before Lambda invocation saves money. Attackers should hit 429s at the API Gateway, never reaching compute.

### 7.1 Layer 1: API Gateway Throttling

Built-in, no additional cost. Configured via Usage Plans.

| Setting | Value |
|---|---|
| Global rate limit | 10 requests/second (burst: 20) |
| Global daily quota | 5,000 requests/day per API key |
| POST /thoughts | 2 requests/minute per user |
| GET endpoints | 30 requests/minute per user |

### 7.2 Layer 2: AWS WAF

Web Application Firewall attached to the API Gateway stage. Cost: ~$5/month base + $0.60/million requests inspected.

- **Rate-based rule:** Block IPs exceeding 100 requests per 5 minutes
- **AWS Managed Rules — Common Rule Set:** Blocks known SQL injection, XSS, and path traversal patterns
- **AWS Managed Rules — Bot Control:** Identifies and throttles automated traffic
- **Geo-restriction (optional):** Block non-US IPs during early access to reduce attack surface

### 7.3 Layer 3: Cognito JWT Authentication

All endpoints except `/auth/*` require a valid Cognito JWT. API Gateway's built-in Cognito authorizer validates the token before invoking Lambda — invalid/expired tokens are rejected at zero compute cost.

- Access tokens: 1-hour expiry
- Refresh tokens: 30-day expiry
- Cognito advanced security: adaptive authentication rate-limits failed logins per account
- Email verification required before first thought submission

### 7.4 Layer 4: Application-Level Guards

Inside the Lambda function, using the following libraries:

| Library | Purpose |
|---|---|
| aws-lambda-powertools (Python) | Structured logging, tracing, idempotency decorator, event parsing |
| Pydantic v2 | Input validation and sanitization (≤280 chars, strip control characters, reject empty) |
| DynamoDB conditional writes | Enforce one-thought-per-day at the database level (ConditionalCheckFailedException → 409) |

**Input Sanitization**

```python
from pydantic import BaseModel, Field, field_validator
import re

class ThoughtInput(BaseModel):
    text: str = Field(min_length=1, max_length=280)

    @field_validator('text')
    def sanitize(cls, v):
        v = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', v)  # strip control chars
        v = v.strip()
        if len(v) == 0:
            raise ValueError('empty after sanitization')
        return v
```

**Prompt Injection Defense**

User thought text is passed to the Haiku canonicalization prompt inside XML tags (`<user_thought>...</user_thought>`) and the system prompt explicitly instructs the model to treat the content as opaque data, not instructions. This is defense-in-depth — even a successful injection only affects a single canonical thought's text, not system behavior.

### 7.5 Layer 5: Cost Alarms & Kill Switch

Non-negotiable. Set up before deploying any other component.

| Trigger | Action |
|---|---|
| AWS Budget: $5/month | Email alert via SNS |
| AWS Budget: $15/month | Email + SMS alert |
| AWS Budget: $25/month | SNS triggers Lambda that disables API Gateway stage (kill switch) |
| CloudWatch: Lambda concurrent executions > 50 | Alarm + notification |
| CloudWatch: Lambda invocations > 10,000/hour | Alarm + notification |
| CloudWatch: DynamoDB consumed WCU > 100/second | Alarm + notification |

The $25 kill switch means the worst-case AWS bill from a DDoS or runaway process is $25, not $25,000. The kill switch Lambda disables the API Gateway deployment stage, which immediately stops all traffic without deleting any configuration.

---

## 8. Full Cost Model

### 8.1 Per-Component Breakdown

| Component | 100 DAU | 1K DAU | 10K DAU | 100K DAU |
|---|---|---|---|---|
| Lambda (invocations + duration) | $0.50 | $3.00 | $20.00 | $80.00 |
| API Gateway | $0.10 | $1.00 | $8.00 | $35.00 |
| DynamoDB (on-demand) | $0.02 | $0.15 | $1.50 | $15.00 |
| Pinecone Serverless | $0.00 | $0.00 | $2.00 | $15.00 |
| Cognito | $0.00 | $0.00 | $0.00 | $0.00* |
| S3 (model weights) | $0.05 | $0.05 | $0.05 | $0.05 |
| Anthropic API (Haiku) | $0.05 | $0.30 | $2.00 | $8.00 |
| AWS WAF | $5.00 | $5.00 | $5.60 | $7.00 |
| CloudWatch + Budgets | $0.00 | $0.00 | $1.00 | $3.00 |
| **TOTAL** | **$5.72** | **$9.50** | **$40.15** | **$163.05** |

\* Cognito is free for the first 50,000 monthly active users. Beyond 50K MAU: $0.0055/MAU.

WAF is the floor cost at low scale ($5/month). Without WAF, total at 100 DAU drops to $0.72/month. WAF is optional for private beta but strongly recommended before any public launch.

### 8.2 Cost Scaling Properties

- **Sublinear LLM costs:** Canonicalization calls decrease as the index matures. New canonical creation rate drops from ~30% of submissions (early) to <5% (mature index with 50K+ canonicals).
- **Linear compute costs:** Lambda and API Gateway scale linearly with request volume. No cost cliffs.
- **Near-zero storage costs:** DynamoDB and Pinecone storage are negligible at all realistic scales. 1M thoughts < 1GB.
- **No baseline compute cost:** Zero traffic = zero cost (minus WAF if enabled). No idle instances.

---

## 9. Deployment & Infrastructure as Code (AWS CDK)

### 9.1 Stack Overview

All AWS infrastructure is defined in TypeScript using AWS CDK v2. CDK synthesizes CloudFormation under the hood but gives you real programming constructs — loops, conditionals, type safety, and L2/L3 constructs that handle IAM, resource linking, and boilerplate automatically. The Pinecone index is managed via a CDK custom resource that calls the Pinecone API during deployment. The Lambda function is packaged as a Docker container image pushed to ECR.

| Tool | Purpose |
|---|---|
| AWS CDK v2 (TypeScript) | IaC for all AWS resources, L2 constructs auto-wire IAM and resource refs |
| `aws-cdk-lib` | Lambda, API Gateway, DynamoDB, Cognito, WAF, S3, IAM, CloudWatch, SNS, Budgets |
| CDK Custom Resource | Pinecone serverless index lifecycle (create/delete via API) |
| GitHub Actions | CI/CD: `cdk diff` on PR, `cdk deploy` on merge to `main` |
| Docker + ECR | Lambda packaging (onnxruntime + model weights + application code) |

### 9.2 Why CDK Over Terraform

The decision is pragmatic for this project:

- **IAM auto-wiring:** `table.grantReadWriteData(fn)` replaces 20+ lines of IAM policy JSON. For a solo dev shipping fast, this is the single biggest time saver.
- **L2 constructs:** `RestApi`, `LambdaIntegration`, `CognitoUserPoolsAuthorizer` — each one collapses 3-5 Terraform resources into a single object with sane defaults.
- **Same language as logic:** Your CDK stack is TypeScript, your CI/CD scripts are YAML calling TypeScript. No context-switching to HCL. If you later add a Next.js frontend, the whole repo is one language ecosystem.
- **Refactoring:** Extract a `SeedApiStack` into a construct library, compose stacks for dev/staging/prod with `new SeedApiStack(app, 'prod', { env: prodEnv })`. Terraform modules achieve the same thing but with more friction.
- **Tradeoff acknowledged:** CDK generates CloudFormation, so you're subject to CF's limits (500 resources per stack, slower rollbacks than Terraform's state-based approach). At your P0 scale (~15 resources), this is irrelevant.

### 9.3 Repository Layout

```
seed-backend/
├── infra/
│   ├── bin/
│   │   └── app.ts              # CDK app entry point, stack instantiation
│   ├── lib/
│   │   ├── seed-api-stack.ts   # Main stack: DDB, Lambda, APIGW, Cognito
│   │   ├── security-stack.ts   # WAF, budget alarms, kill switch
│   │   └── pinecone-custom-resource.ts  # Custom resource for Pinecone index
│   ├── cdk.json
│   ├── tsconfig.json
│   └── package.json
├── src/
│   ├── handler.py              # Lambda entry point
│   ├── embedding.py            # ONNX inference
│   ├── canonicalization.py     # Haiku LLM calls
│   ├── models.py               # Pydantic schemas
│   └── requirements.txt
├── Dockerfile                  # Lambda container image
├── .github/
│   └── workflows/
│       └── deploy.yml          # Diff on PR, deploy on merge
└── README.md
```

### 9.4 CDK App Entry Point (`bin/app.ts`)

```typescript
import * as cdk from 'aws-cdk-lib';
import { SeedApiStack } from '../lib/seed-api-stack';
import { SecurityStack } from '../lib/security-stack';

const app = new cdk.App();

const env = app.node.tryGetContext('env') || 'dev';
const awsEnv = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: 'us-east-1',
};

const apiStack = new SeedApiStack(app, `Seed-Api-${env}`, {
  env: awsEnv,
  stageName: env,
  similarityThresholdAuto: '0.85',
  similarityThresholdMin: '0.60',
  modelS3Key: 'minilm-l6-v2.onnx',
});

new SecurityStack(app, `Seed-Security-${env}`, {
  env: awsEnv,
  stageName: env,
  apiGateway: apiStack.api,
  lambdaFunction: apiStack.lambdaFn,
  dynamoTable: apiStack.table,
  alertEmail: app.node.tryGetContext('alertEmail'),
});
```

### 9.5 Main Stack (`lib/seed-api-stack.ts`)

```typescript
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';

interface SeedApiStackProps extends cdk.StackProps {
  stageName: string;
  similarityThresholdAuto: string;
  similarityThresholdMin: string;
  modelS3Key: string;
}

export class SeedApiStack extends cdk.Stack {
  public readonly api: apigateway.RestApi;
  public readonly lambdaFn: lambda.Function;
  public readonly table: dynamodb.Table;

  constructor(scope: Construct, id: string, props: SeedApiStackProps) {
    super(scope, id, props);

    // ── DynamoDB ──
    this.table = new dynamodb.Table(this, 'SeedPrimary', {
      tableName: `seed-primary-${props.stageName}`,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: props.stageName === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    });

    this.table.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── S3: Model Weights ──
    const modelBucket = new s3.Bucket(this, 'ModelWeights', {
      bucketName: `seed-models-${props.stageName}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    // ── Secrets Manager ──
    const pineconeSecret = secretsmanager.Secret.fromSecretNameV2(
      this, 'PineconeKey', `seed/pinecone-api-key-${props.stageName}`
    );
    const anthropicSecret = secretsmanager.Secret.fromSecretNameV2(
      this, 'AnthropicKey', `seed/anthropic-api-key-${props.stageName}`
    );

    // ── Cognito ──
    const userPool = new cognito.UserPool(this, 'SeedUsers', {
      userPoolName: `seed-users-${props.stageName}`,
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: props.stageName === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = userPool.addClient('SeedClient', {
      authFlows: { userSrp: true },
      accessTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    // ── ECR Repository ──
    const ecrRepo = new ecr.Repository(this, 'LambdaRepo', {
      repositoryName: `seed-lambda-${props.stageName}`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
    });

    // ── Lambda ──
    this.lambdaFn = new lambda.DockerImageFunction(this, 'SeedApi', {
      functionName: `seed-api-${props.stageName}`,
      code: lambda.DockerImageCode.fromEcr(ecrRepo, {
        tagOrDigest: process.env.IMAGE_TAG || 'latest',
      }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 1024,
      timeout: cdk.Duration.seconds(30),
      environment: {
        DDB_TABLE_NAME: this.table.tableName,
        COGNITO_USER_POOL_ID: userPool.userPoolId,
        MODEL_S3_BUCKET: modelBucket.bucketName,
        MODEL_S3_KEY: props.modelS3Key,
        PINECONE_API_KEY_SECRET: pineconeSecret.secretArn,
        ANTHROPIC_API_KEY_SECRET: anthropicSecret.secretArn,
        SIMILARITY_THRESHOLD_AUTO: props.similarityThresholdAuto,
        SIMILARITY_THRESHOLD_MIN: props.similarityThresholdMin,
      },
    });

    // IAM — CDK auto-generates least-privilege policies
    this.table.grantReadWriteData(this.lambdaFn);
    modelBucket.grantRead(this.lambdaFn);
    pineconeSecret.grantRead(this.lambdaFn);
    anthropicSecret.grantRead(this.lambdaFn);

    // ── API Gateway ──
    this.api = new apigateway.RestApi(this, 'SeedRestApi', {
      restApiName: `seed-api-${props.stageName}`,
      description: 'SEED Thought Journaling API',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: {
        stageName: props.stageName,
        throttlingRateLimit: 10,
        throttlingBurstLimit: 20,
      },
    });

    const cognitoAuthorizer = new apigateway.CognitoUserPoolsAuthorizer(
      this, 'CognitoAuth', { cognitoUserPools: [userPool] }
    );

    const lambdaIntegration = new apigateway.LambdaIntegration(this.lambdaFn);

    // Auth routes (no authorizer)
    const auth = this.api.root.addResource('auth');
    auth.addResource('signup').addMethod('POST', lambdaIntegration);
    auth.addResource('login').addMethod('POST', lambdaIntegration);
    auth.addResource('refresh').addMethod('POST', lambdaIntegration);

    // Protected routes
    const authOpts = {
      authorizer: cognitoAuthorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    };

    const thoughts = this.api.root.addResource('thoughts');
    thoughts.addMethod('POST', lambdaIntegration, authOpts);

    const thoughtsMine = thoughts.addResource('mine');
    thoughtsMine.addMethod('GET', lambdaIntegration, authOpts);

    const thoughtsToday = thoughts.addResource('today');
    thoughtsToday.addMethod('GET', lambdaIntegration, authOpts);

    const thoughtsConfirm = thoughts.addResource('confirm');
    thoughtsConfirm.addMethod('POST', lambdaIntegration, authOpts);

    const rooms = this.api.root.addResource('rooms');
    const roomById = rooms.addResource('{canonical_id}');
    roomById.addMethod('GET', lambdaIntegration, authOpts);

    const roomThoughts = roomById.addResource('thoughts');
    roomThoughts.addMethod('GET', lambdaIntegration, authOpts);

    // Usage Plan
    const usagePlan = this.api.addUsagePlan('StandardPlan', {
      name: `seed-standard-${props.stageName}`,
      throttle: { rateLimit: 10, burstLimit: 20 },
      quota: { limit: 5000, period: apigateway.Period.DAY },
    });
    usagePlan.addApiStage({ stage: this.api.deploymentStage });

    // ── Outputs ──
    new cdk.CfnOutput(this, 'ApiUrl', { value: this.api.url });
    new cdk.CfnOutput(this, 'UserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: userPoolClient.userPoolClientId });
    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'EcrRepoUri', { value: ecrRepo.repositoryUri });
  }
}
```

### 9.6 Security Stack (`lib/security-stack.ts`)

```typescript
import * as cdk from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

interface SecurityStackProps extends cdk.StackProps {
  stageName: string;
  apiGateway: apigateway.RestApi;
  lambdaFunction: lambda.Function;
  dynamoTable: dynamodb.Table;
  alertEmail: string;
}

export class SecurityStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SecurityStackProps) {
    super(scope, id, props);

    // ── SNS Alerts Topic ──
    const alertTopic = new sns.Topic(this, 'AlertTopic', {
      topicName: `seed-alerts-${props.stageName}`,
    });
    alertTopic.addSubscription(
      new subscriptions.EmailSubscription(props.alertEmail)
    );

    // ── WAF ──
    const webAcl = new wafv2.CfnWebACL(this, 'SeedWaf', {
      name: `seed-waf-${props.stageName}`,
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: `seed-waf-${props.stageName}`,
      },
      rules: [
        {
          name: 'rate-limit',
          priority: 1,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 100,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'seed-rate-limit',
          },
        },
        {
          name: 'aws-common-rules',
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'seed-common-rules',
          },
        },
        {
          name: 'aws-bot-control',
          priority: 3,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesBotControlRuleSet',
            },
          },
          visibilityConfig: {
            sampledRequestsEnabled: true,
            cloudWatchMetricsEnabled: true,
            metricName: 'seed-bot-control',
          },
        },
      ],
    });

    // Associate WAF with API Gateway stage
    new wafv2.CfnWebACLAssociation(this, 'WafAssociation', {
      resourceArn: props.apiGateway.deploymentStage.stageArn,
      webAclArn: webAcl.attrArn,
    });

    // ── CloudWatch Alarms ──
    const lambdaConcurrency = new cloudwatch.Alarm(this, 'LambdaConcurrency', {
      alarmName: `seed-lambda-concurrency-${props.stageName}`,
      metric: props.lambdaFunction.metric('ConcurrentExecutions', {
        statistic: 'Maximum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 50,
      evaluationPeriods: 1,
    });
    lambdaConcurrency.addAlarmAction(new cw_actions.SnsAction(alertTopic));

    const lambdaInvocations = new cloudwatch.Alarm(this, 'LambdaInvocations', {
      alarmName: `seed-lambda-invocations-${props.stageName}`,
      metric: props.lambdaFunction.metric('Invocations', {
        statistic: 'Sum',
        period: cdk.Duration.hours(1),
      }),
      threshold: 10000,
      evaluationPeriods: 1,
    });
    lambdaInvocations.addAlarmAction(new cw_actions.SnsAction(alertTopic));

    const ddbWrites = new cloudwatch.Alarm(this, 'DdbWrites', {
      alarmName: `seed-ddb-wcu-${props.stageName}`,
      metric: props.dynamoTable.metric('ConsumedWriteCapacityUnits', {
        statistic: 'Sum',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 6000, // 100 WCU/sec * 60 sec
      evaluationPeriods: 1,
    });
    ddbWrites.addAlarmAction(new cw_actions.SnsAction(alertTopic));

    // ── AWS Budget — $25 Kill Switch ──
    new budgets.CfnBudget(this, 'MonthlyCap', {
      budget: {
        budgetName: `seed-monthly-cap-${props.stageName}`,
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: 25, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 20, // $5
          },
          subscribers: [{ subscriptionType: 'EMAIL', address: props.alertEmail }],
        },
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 60, // $15
          },
          subscribers: [{ subscriptionType: 'EMAIL', address: props.alertEmail }],
        },
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: 100, // $25 — triggers kill switch via SNS → Lambda
          },
          subscribers: [{ subscriptionType: 'SNS', address: alertTopic.topicArn }],
        },
      ],
    });
  }
}
```

### 9.7 Pinecone Custom Resource (`lib/pinecone-custom-resource.ts`)

Pinecone doesn't have a native CDK construct, so we use a CDK Custom Resource backed by an inline Lambda that calls the Pinecone API to create/delete the index during `cdk deploy` / `cdk destroy`.

```typescript
import * as cdk from 'aws-cdk-lib';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

interface PineconeIndexProps {
  indexName: string;
  dimension: number;
  metric: string;
  cloud: string;
  region: string;
  apiKeySecretArn: string;
}

export class PineconeIndex extends Construct {
  constructor(scope: Construct, id: string, props: PineconeIndexProps) {
    super(scope, id);

    const onEvent = new cdk.aws_lambda.Function(this, 'PineconeHandler', {
      runtime: cdk.aws_lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: cdk.aws_lambda.Code.fromInline(`
import json, urllib.request, boto3, os

def handler(event, context):
    sm = boto3.client('secretsmanager')
    api_key = sm.get_secret_value(
        SecretId=os.environ['API_KEY_SECRET_ARN']
    )['SecretString']

    props = event['ResourceProperties']
    req_type = event['RequestType']

    if req_type in ('Create', 'Update'):
        body = json.dumps({
            'name': props['IndexName'],
            'dimension': int(props['Dimension']),
            'metric': props['Metric'],
            'spec': {
                'serverless': {
                    'cloud': props['Cloud'],
                    'region': props['Region']
                }
            }
        }).encode()
        req = urllib.request.Request(
            'https://api.pinecone.io/indexes',
            data=body,
            headers={
                'Api-Key': api_key,
                'Content-Type': 'application/json',
                'X-Pinecone-API-Version': '2024-10'
            },
            method='POST'
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            if e.code == 409:  # already exists
                pass
            else:
                raise

    elif req_type == 'Delete':
        req = urllib.request.Request(
            f"https://api.pinecone.io/indexes/{props['IndexName']}",
            headers={'Api-Key': api_key},
            method='DELETE'
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError:
            pass

    return {'PhysicalResourceId': props['IndexName']}
      `),
      environment: {
        API_KEY_SECRET_ARN: props.apiKeySecretArn,
      },
      timeout: cdk.Duration.seconds(30),
    });

    // Grant Secrets Manager access
    cdk.aws_iam.Grant.addToPrincipal({
      grantee: onEvent,
      actions: ['secretsmanager:GetSecretValue'],
      resourceArns: [props.apiKeySecretArn],
    });

    new cr.Provider(this, 'PineconeProvider', {
      onEventHandler: onEvent,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    new cdk.CustomResource(this, 'PineconeIndexResource', {
      serviceToken: new cr.Provider(this, 'Provider', {
        onEventHandler: onEvent,
        logRetention: logs.RetentionDays.ONE_WEEK,
      }).serviceToken,
      properties: {
        IndexName: props.indexName,
        Dimension: props.dimension.toString(),
        Metric: props.metric,
        Cloud: props.cloud,
        Region: props.region,
      },
    });
  }
}
```

Usage in `seed-api-stack.ts`:

```typescript
new PineconeIndex(this, 'SeedCanonicalsIndex', {
  indexName: `seed-canonicals-${props.stageName}`,
  dimension: 384,
  metric: 'cosine',
  cloud: 'aws',
  region: 'us-east-1',
  apiKeySecretArn: pineconeSecret.secretArn,
});
```

### 9.8 CI/CD Pipeline (GitHub Actions)

```yaml
# .github/workflows/deploy.yml
name: Deploy SEED Backend

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  AWS_REGION: us-east-1

jobs:
  diff:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - run: cd infra && npm ci

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ env.AWS_REGION }}

      - run: cd infra && npx cdk diff -c env=dev -c alertEmail=${{ secrets.ALERT_EMAIL }}

  deploy:
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - run: cd infra && npm ci

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ env.AWS_REGION }}

      # Build and push Lambda container image
      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr-login

      - run: |
          docker build -t seed-lambda .
          docker tag seed-lambda:latest ${{ steps.ecr-login.outputs.registry }}/seed-lambda-prod:${{ github.sha }}
          docker push ${{ steps.ecr-login.outputs.registry }}/seed-lambda-prod:${{ github.sha }}

      # CDK deploy
      - run: |
          cd infra && npx cdk deploy --all --require-approval never \
            -c env=prod \
            -c alertEmail=${{ secrets.ALERT_EMAIL }}
        env:
          IMAGE_TAG: ${{ github.sha }}
```

### 9.9 CDK Bootstrap & First Deploy

```bash
# One-time: bootstrap CDK in your AWS account/region
cd infra
npm install
npx cdk bootstrap aws://ACCOUNT_ID/us-east-1

# First deploy (dev)
npx cdk deploy --all -c env=dev -c alertEmail=you@example.com

# Diff before deploying changes
npx cdk diff -c env=dev -c alertEmail=you@example.com

# Destroy (dev only)
npx cdk destroy --all -c env=dev
```

### 9.10 Migration Path

Each component is independently replaceable as the system scales:

| Trigger | Migration |
|---|---|
| Cold starts unacceptable | Add 1 provisioned concurrency ($15/mo) or migrate to Fargate |
| Canonicals > 100K | Evaluate whether Pinecone free tier still covers; likely yes up to ~500K |
| Need full-text search on canonicals | Add OpenSearch for text search alongside Pinecone for vectors |
| Haiku costs > $50/mo | Self-host Qwen2.5-1.5B on a g5.xlarge spot instance |
| Multi-region needed | DynamoDB Global Tables + Pinecone multi-region replication |
| Need advanced analytics | Stream DynamoDB changes to S3 via Kinesis, query with Athena |

---

## 10. Open Questions for V1.1+

- **Canonical mutation policy:** When should a canonical's text be re-generalized as more thoughts link to it? Immutable canonicals are simpler but may drift from the centroid of their linked thoughts over time. A mutation trigger (e.g., every 50 new links, re-generalize from a sample of linked thoughts) would improve match quality but adds complexity.

- **Embedding model upgrade path:** Swapping from MiniLM to a better model (bge, e5, or a future model) invalidates all existing vectors. Plan: store raw text in DynamoDB, run a batch re-embedding job, re-index Pinecone. The generalized canonical text is the source of truth, not the embedding.

- **Room privacy model:** When users browse a room, do they see other users' raw thoughts or only the canonical + count? P0 shows only canonical text + count. Exposing raw thoughts requires consent model and moderation.

- **Moderation pipeline:** User-submitted thoughts may contain hate speech, PII, or harmful content. The canonicalization LLM provides a natural moderation checkpoint (refuse to generalize harmful content), but raw thoughts in DynamoDB still need a content policy.

- **Offline/batch embedding:** If cold starts prove too painful, consider an async architecture: user submits thought → Lambda writes to DDB as "pending" → SQS triggers a batch Lambda every 5 minutes that processes all pending thoughts. Trades real-time feedback for consistent latency.