# COMP 7940 Group Project
A highly scalable, available, and secure Telegram chatbot system with multiple LLM backends, supporting automatic failover and independent deployment of frontend and backend.
Main Features
High Scalability
Backend

Support different backend containers with independent configurations:
API_VER
OPENAI_BASE_URL
OPENAI_API_KEY


Compatible with multiple AI platforms and models, including DeepSeek, ChatGPT, Gemini, etc.
Backend servers can be replaced or added in real time and take effect immediately without code modification.

Frontend

Fully independent from backend services.
Frontend can be installed and run regardless of backend status.

High Availability

After receiving a message, the frontend uses the backend set by /setllm x as priority.
If the current backend returns an error or is unavailable, the system automatically tries x+1, x+2, …, until the last backend.
Default initial backend: /setllm 1

Telegram Frontend Commands

/setllm 1 uses deploy-backend-1 on port 8001
/setllm 2 uses deploy-backend-2 on port 8002
…
If the corresponding 800x host does not exist, an error message is returned.
Maximum configurable backend ID in frontend (default: 20)

High Security

No local or repository file modification required when adding frontends or backends.
All keys and credentials are injected only during GitHub Actions runtime.
No secrets stored locally or in the GitHub repository.

DevOps Compliance

Full deployment via GitHub Actions using secrets:
EC2_HOST
EC2_KEYS
EC2_USER


Database accessible via MongoDB Compass without SSH.
Entire deployment pipeline follows standard DevOps practices.

Getting Started

Configure required secrets in GitHub repository settings.
Run deployment workflows in GitHub Actions.
Interact with the bot via Telegram using /setllm to switch LLM backends.
Monitor and manage the database using MongoDB Compass.
