"""Role families and skill names used to ground CV generation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Domain:
    area: str
    title: str
    keywords: tuple[str, ...]


# Fixed career order: AI leadership, then project management, then BA / forward
# deployed work, then engineering as the execution layer.
DOMAINS: tuple[Domain, ...] = (
    Domain(
        "ai_director_architect",
        "AI Director and Architect",
        ("llm", "agent", "mcp", "prompt", "architect", "ai", "innovation"),
    ),
    Domain(
        "technical_project_management",
        "Technical Project Manager",
        ("project management", "roadmap", "planning", "sdlc", "agile", "operations", "management"),
    ),
    Domain(
        "business_analyst_forward_deployed_engineer",
        "Business Analyst and Forward Deployed Engineer",
        ("business analysis", "stakeholder", "requirements", "uat", "onsite", "deployment"),
    ),
    Domain(
        "software_engineer",
        "Software Engineer",
        ("python", "sql", "api", "react", "typescript", "fastapi", "node.js"),
    ),
)

SKILL_NAMES: dict[str, str] = {
    "python": "Python",
    "java": "Java",
    "go": "Go",
    "sql": "SQL",
    "api": "API",
    "backend": "Backend",
    "docker": "Docker",
    "django": "Django",
    "flask": "Flask",
    "ai": "AI",
    "architect": "Architect",
    "innovation": "Innovation",
    "project management": "Project Management",
    "roadmap": "Roadmap",
    "planning": "Planning",
    "sdlc": "SDLC",
    "agile": "Agile",
    "operations": "Operations",
    "management": "Management",
    "business analysis": "Business Analysis",
    "stakeholder": "Stakeholder",
    "requirements": "Requirements",
    "uat": "UAT",
    "onsite": "Onsite",
    "deployment": "Deployment",
    "llm": "LLM",
    "agent": "Agent",
    "langchain": "LangChain",
    "rag": "RAG",
    "openai": "OpenAI",
    "machine learning": "Machine Learning",
    "pytorch": "PyTorch",
    "prompt": "Prompt",
    "react": "React",
    "typescript": "TypeScript",
    "css": "CSS",
    "frontend": "Frontend",
    "vue": "Vue",
    "next.js": "Next.js",
    "javascript": "JavaScript",
    "html": "HTML",
    "full stack": "Full Stack",
    "node.js": "Node.js",
    "postgres": "Postgres",
    "spark": "Spark",
    "etl": "ETL",
    "airflow": "Airflow",
    "warehouse": "Warehouse",
    "dbt": "dbt",
    "kafka": "Kafka",
    "pipeline": "Pipeline",
    "aws": "AWS",
    "gcp": "GCP",
    "azure": "Azure",
    "kubernetes": "Kubernetes",
    "git": "Git",
    "linux": "Linux",
    "fastapi": "FastAPI",
    "mcp": "MCP",
    "websocket": "WebSocket",
    "react native": "React Native",
    "mongodb": "MongoDB",
    "mysql": "MySQL",
    "pandas": "Pandas",
    "redis": "Redis",
    "graphql": "GraphQL",
    "terraform": "Terraform",
    "ci/cd": "CI/CD",
}
