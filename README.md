# COMP 7940 Group Project

This is the repository for the COMP 7940 Group Project, a full-stack project with a LLM-based backend and a dedicated frontend, designed with containerization support and automated deployment workflows.
Project Structure
The project is modularly organized into backend, frontend, and DevOps configuration directories for clear separation of concerns and maintainability:
plaintext

  comp_7940_gp/
    ├── .github/
    │   └── workflows/       # CI/CD workflow configurations (e.g., frontend deployment)
    ├── backend/
    │   └── llm-backend/     # LLM-powered backend core logic
    ├── frontend/            # Frontend application code
    ├── README.md            # Project documentation
    └── [Dockerfile(s)]      # Containerization configuration (4.4% of project code)

Tech Stack
Primary Language: Python (95.6%) - Core development for the LLM backend
Containerization: Docker (Dockerfile, 4.4%) - For consistent deployment and environment isolation
DevOps: GitHub Actions - Automated workflows for frontend deployment and project updates
Architecture: Full-stack (LLM backend + custom frontend)
