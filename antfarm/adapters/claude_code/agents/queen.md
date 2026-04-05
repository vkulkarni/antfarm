# Antfarm Queen — Claude Code Agent (Example Only)

> **This is an example, not a core platform feature.** The Queen role in v0.1 is a human or automation that calls `antfarm carry` to decompose work into tasks. This agent definition shows what an AI-powered Queen *could* look like.

## What the Queen Does

The Queen receives a high-level specification and breaks it into concrete tasks, then enqueues them via `antfarm carry`.

## Example: Decomposing a Feature Spec

Given a spec like:

> "Add user authentication: sign-up, login, JWT issuance, and a /me endpoint."

An AI Queen would reason through the subtasks and call:

```bash
antfarm carry --title "Add users table migration" \
  --description "Alembic migration: create users table with id, email, hashed_password, created_at" \
  --branch "feat/auth-users-migration"

antfarm carry --title "Implement JWT issuance" \
  --description "Create core/auth.py: hash password, verify, issue HS256 JWT with 24h expiry" \
  --branch "feat/auth-jwt"

antfarm carry --title "Add /auth/signup and /auth/login endpoints" \
  --description "FastAPI router: POST /auth/signup (create user), POST /auth/login (return JWT)" \
  --branch "feat/auth-endpoints"

antfarm carry --title "Add /users/me endpoint" \
  --description "GET /users/me: decode JWT, return user profile. Requires auth middleware." \
  --branch "feat/auth-me-endpoint"
```

Each `antfarm carry` call creates one task in the queue. Workers then claim and implement them independently.

## Why This Is an Example

In v0.1, task decomposition is done by humans. The Queen agent definition is included here to:

1. Show how AI decomposition *could* integrate with the platform
2. Serve as a starting point for a v0.2 AI-Queen feature
3. Document the `antfarm carry` interface for reference

To use this pattern today, run the `antfarm carry` commands yourself (or in a script) rather than having an AI agent do it.
