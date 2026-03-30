# COMP 7940 Group Project
A scalable, highly available, and secure Telegram chatbot system with multiple LLM backends, supporting automatic failover, independent frontend-backend deployment, and compliant DevOps workflows.
Project Overview

This repository hosts the COMP 7940 Group Project, a full-stack system consisting of an LLM-powered backend, a Telegram-based frontend, and CI/CD deployment configurations. The system is designed for scalability, high availability, security, and adherence to DevOps best practices.

# Tech Stack

- Languages: Python (95.6%), Dockerfile (4.4%)

- Backend: LLM integration (supports DeepSeek, ChatGPT, Gemini, etc.)

- Frontend: Telegram bot (independent of backend)

- DevOps: Docker, GitHub Actions, MongoDB

- Deployment: EC2 (via GitHub Actions secrets)

# Main Features

# 1. High Scalability

Backend

- Each backend container supports independent configurations: API_VER, OPENAI_BASE_URL, OPENAI_API_KEY

- Compatible with multiple AI platforms and models (DeepSeek, ChatGPT, Gemini, etc.)

- Backend servers can be added or replaced in real time, taking effect immediately without code modifications.

Frontend

- Fully independent from backend services; can be installed and run without relying on backend availability.

# 2. High Availability

- After receiving a message, the frontend prioritizes the backend specified by /setllm x.

- If the prioritized backend returns an error or is unavailable, the system automatically tries the next backend (x+1, x+2, ...) until the last available backend.

- Default initial backend: /setllm 1

Telegram Frontend Commands

- /setllm 1: Uses deploy-backend-1 (port 8001)

- /setllm 2: Uses deploy-backend-2 (port 8002)

- Error returned if the corresponding 800x host does not exist.

- Configurable maximum backend ID (default: 20).

# 3. High Security

- No file modifications required when adding frontends or backends.

- All keys and credentials are injected only during GitHub Actions runtime.

- No secrets (keys, credentials) are stored locally or in the GitHub repository.

# 4. DevOps Compliance

- Full deployment via GitHub Actions using the following secrets (added to repository settings):
       

  - EC2_HOST

  - EC2_KEYS

  - EC2_USER

- Database accessible via MongoDB Compass without SSH.

- End-to-end deployment pipeline follows standard DevOps practices.

# Getting Started

1. Add the required secrets (EC2_HOST,EC2_KEYS, EC2_USER) in your GitHub repository settings.

2. Run the deployment workflow in GitHub Actions to deploy frontends and backends.

3. Interact with the Telegram bot: Use /setllm x to switch between backend servers (x = backend ID).

4. Monitor and manage the database using MongoDB Compass (no SSH required).

# Project Structure

- `comp_7940_gp/`
  - `.github/workflows/` - CI/CD automation scripts (e.g., frontend-deploy.yml)
  - `backend/`
    - `llm-backend/` - LLM backend service (multiple containers supported)
  - `frontend/` - Telegram frontend (independent of backend)
  - `Dockerfile` - Containerization configuration
  - `README.md` - Project documentation
