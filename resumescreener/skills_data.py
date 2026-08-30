"""
Curated dictionary of 200+ technical and soft skills organized by category.
Used by the NLP engine to extract and match skills from resumes and job descriptions.
"""

TECHNICAL_SKILLS = {
    "programming_languages": [
        "python", "java", "javascript", "typescript", "c", "c++", "c#", "ruby",
        "go", "golang", "rust", "swift", "kotlin", "php", "perl", "scala",
        "r", "matlab", "dart", "lua", "haskell", "elixir", "clojure",
        "objective-c", "visual basic", "vba", "assembly", "fortran", "cobol",
        "julia", "groovy", "shell", "bash", "powershell", "sql", "plsql"
    ],
    "web_frameworks": [
        "react", "reactjs", "react.js", "angular", "angularjs", "vue", "vuejs",
        "vue.js", "nextjs", "next.js", "nuxt", "nuxtjs", "svelte", "django",
        "flask", "fastapi", "express", "expressjs", "express.js", "spring",
        "spring boot", "springboot", "rails", "ruby on rails", "laravel",
        "asp.net", ".net", "dotnet", "gin", "fiber", "nest", "nestjs",
        "gatsby", "remix", "astro", "ember", "backbone"
    ],
    "mobile": [
        "react native", "flutter", "swift", "swiftui", "kotlin", "android",
        "ios", "xamarin", "ionic", "cordova", "expo"
    ],
    "databases": [
        "mysql", "postgresql", "postgres", "mongodb", "redis", "sqlite",
        "oracle", "sql server", "mssql", "cassandra", "dynamodb", "couchdb",
        "neo4j", "elasticsearch", "mariadb", "firestore", "firebase",
        "supabase", "cockroachdb", "influxdb", "timescaledb"
    ],
    "cloud_devops": [
        "aws", "amazon web services", "azure", "gcp", "google cloud",
        "google cloud platform", "docker", "kubernetes", "k8s", "terraform",
        "ansible", "jenkins", "ci/cd", "cicd", "github actions", "gitlab ci",
        "circleci", "travis ci", "heroku", "vercel", "netlify", "digitalocean",
        "cloudflare", "nginx", "apache", "linux", "unix", "helm", "argocd",
        "prometheus", "grafana", "datadog", "new relic", "splunk", "elk",
        "vagrant", "puppet", "chef"
    ],
    "runtimes_apis": [
        "node", "node.js", "nodejs", "deno", "bun", "graphql", "rest", "rest api",
        "grpc", "websocket", "websockets", "openapi", "swagger", "soap", "webhooks",
        "protobuf", "protocol buffers", "json", "xml", "oauth", "jwt", "microservices",
        "serverless", "message queue", "kafka", "rabbitmq", "sqs", "celery", "redis queue"
    ],
    "observability": [
        "elk", "elastic stack", "filebeat", "logstash", "kibana", "beats",
        "opentelemetry", "otel", "jaeger", "zipkin", "loki", "fluentd", "fluent bit",
        "sentry", "pagerduty", "sre", "observability", "distributed tracing"
    ],
    "data_science_ml": [
        "machine learning", "deep learning", "artificial intelligence", "ai",
        "ml", "nlp", "natural language processing", "computer vision",
        "tensorflow", "pytorch", "keras", "scikit-learn", "sklearn",
        "pandas", "numpy", "scipy", "matplotlib", "seaborn", "plotly",
        "opencv", "spacy", "nltk", "hugging face", "huggingface",
        "transformers", "bert", "gpt", "llm", "large language model",
        "neural network", "cnn", "rnn", "lstm", "gan", "reinforcement learning",
        "xgboost", "lightgbm", "catboost", "random forest", "regression",
        "classification", "clustering", "pca", "feature engineering",
        "data mining", "data analysis", "data visualization", "tableau",
        "power bi", "looker", "apache spark", "spark", "hadoop", "hive",
        "airflow", "kafka", "dbt", "snowflake", "databricks", "bigquery",
        "etl", "data pipeline", "data warehouse", "data lake"
    ],
    "tools_platforms": [
        "git", "github", "gitlab", "bitbucket", "jira", "confluence",
        "trello", "asana", "slack", "notion", "figma", "sketch", "adobe xd",
        "photoshop", "illustrator", "postman", "swagger", "graphql",
        "rest", "restful", "api", "microservices", "rabbitmq", "celery",
        "websocket", "grpc", "oauth", "jwt", "saml", "sso",
        "webpack", "vite", "babel", "npm", "yarn", "pnpm",
        "pip", "conda", "poetry", "maven", "gradle"
    ],
    "testing": [
        "unit testing", "integration testing", "e2e testing", "jest", "mocha",
        "chai", "cypress", "selenium", "playwright", "puppeteer", "pytest",
        "unittest", "junit", "testng", "rspec", "tdd", "bdd",
        "test driven development", "behavior driven development",
        "load testing", "stress testing", "performance testing"
    ],
    "security": [
        "cybersecurity", "penetration testing", "ethical hacking", "owasp",
        "encryption", "ssl", "tls", "firewall", "ids", "ips", "siem",
        "vulnerability assessment", "security audit", "iso 27001", "gdpr",
        "hipaa", "soc2", "devsecops"
    ]
}

SOFT_SKILLS = [
    "communication", "teamwork", "leadership", "problem solving",
    "problem-solving", "critical thinking", "time management",
    "project management", "agile", "scrum", "kanban", "waterfall",
    "presentation", "public speaking", "negotiation", "mentoring",
    "coaching", "collaboration", "adaptability", "creativity",
    "innovation", "analytical", "strategic thinking", "decision making",
    "conflict resolution", "emotional intelligence", "stakeholder management",
    "cross-functional", "multitasking", "attention to detail",
    "organizational", "self-motivated", "proactive", "results-driven",
    "customer-focused", "client-facing", "vendor management",
    "budget management", "risk management", "change management",
    "process improvement", "continuous improvement", "lean", "six sigma"
]

EDUCATION_KEYWORDS = {
    "degrees": [
        "b.tech", "btech", "b.e", "b.sc", "bsc", "b.s", "bs",
        "bachelor", "bachelors", "bachelor's",
        "m.tech", "mtech", "m.e", "m.sc", "msc", "m.s", "ms",
        "master", "masters", "master's", "mba",
        "ph.d", "phd", "doctorate", "doctoral",
        "associate", "diploma", "certification", "certificate",
        "b.a", "ba", "m.a", "ma", "b.com", "bcom", "m.com", "mcom",
        "bba", "bca", "mca", "llb", "ll.b", "md", "m.d"
    ],
    "fields": [
        "computer science", "computer engineering", "software engineering",
        "information technology", "information systems", "data science",
        "artificial intelligence", "machine learning", "cybersecurity",
        "electrical engineering", "electronics", "mechanical engineering",
        "mathematics", "statistics", "physics", "chemistry", "biology",
        "business administration", "finance", "economics", "marketing",
        "human resources", "management", "accounting", "commerce",
        "liberal arts", "psychology", "sociology", "english",
        "communications", "design", "architecture"
    ]
}

EXPERIENCE_PATTERNS = [
    r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:experience|exp)',
    r'experience\s*(?:of\s+)?(\d+)\+?\s*(?:years?|yrs?)',
    r'(\d+)\+?\s*(?:years?|yrs?)\s+(?:in|of|working)',
    r'(?:over|more than|at least|minimum)\s*(\d+)\+?\s*(?:years?|yrs?)',
]


def get_all_skills():
    """Return a flat set of all technical + soft skills."""
    all_skills = set()
    for category_skills in TECHNICAL_SKILLS.values():
        for skill in category_skills:
            all_skills.add(skill.lower())
    for skill in SOFT_SKILLS:
        all_skills.add(skill.lower())
    return all_skills


def get_skills_by_category():
    """Return skills organized by category for detailed reporting."""
    categories = {}
    for category, skills in TECHNICAL_SKILLS.items():
        display_name = category.replace("_", " ").title()
        categories[display_name] = [s.lower() for s in skills]
    categories["Soft Skills"] = [s.lower() for s in SOFT_SKILLS]
    return categories
