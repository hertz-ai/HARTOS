"""
Expert Agent Registry - Comprehensive Network of Specialized AI Agents

This defines the complete ecosystem of expert agents available for dream fulfillment.
Each agent is a specialist in its domain, with clear capabilities and endpoints.

Philosophy: "Don't build one generalist AI. Build a network of expert AIs
that collaborate like a world-class team."

Agent Categories:
- Software Development (15 agents)
- Data & Analytics (10 agents)
- Creative & Design (12 agents)
- Business & Operations (8 agents)
- Education & Learning (7 agents)
- Health & Wellness (6 agents)
- Communication & Social (8 agents)
- Infrastructure & DevOps (10 agents)
- Research & Analysis (8 agents)
- Specialized Domains (12 agents)

Total: 96 Expert Agents
"""

from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum


class AgentCategory(Enum):
    """Categories of expert agents."""
    SOFTWARE_DEV = "software_development"
    DATA_ANALYTICS = "data_analytics"
    CREATIVE_DESIGN = "creative_design"
    BUSINESS_OPS = "business_operations"
    EDUCATION = "education_learning"
    HEALTH = "health_wellness"
    COMMUNICATION = "communication_social"
    INFRASTRUCTURE = "infrastructure_devops"
    RESEARCH = "research_analysis"
    SPECIALIZED = "specialized_domains"


@dataclass
class AgentCapability:
    """A specific capability an agent has."""
    name: str
    description: str
    example_use: str


@dataclass
class ExpertAgent:
    """Definition of an expert agent."""
    agent_id: str
    name: str
    category: AgentCategory
    description: str
    capabilities: List[AgentCapability]
    endpoint: str
    model_type: str  # "llm", "vision", "audio", "multimodal", "tool"
    cost_per_call: float  # Estimated cost (0 = free/local)
    avg_latency_ms: float  # Average response time
    reliability: float  # 0.0 to 1.0


class ExpertAgentRegistry:
    """
    Central registry of all expert agents.

    This is the "phone book" for the dream fulfillment engine.
    When a dream needs specific expertise, query this registry.
    """

    def __init__(self):
        self.agents: Dict[str, ExpertAgent] = {}
        self._initialize_all_agents()

    def _initialize_all_agents(self):
        """Initialize all expert agents."""
        # Software Development Agents
        self._init_software_dev_agents()

        # Data & Analytics Agents
        self._init_data_analytics_agents()

        # Creative & Design Agents
        self._init_creative_design_agents()

        # Business & Operations Agents
        self._init_business_ops_agents()

        # Education & Learning Agents
        self._init_education_agents()

        # Health & Wellness Agents
        self._init_health_agents()

        # Communication & Social Agents
        self._init_communication_agents()

        # Infrastructure & DevOps Agents
        self._init_infrastructure_agents()

        # Research & Analysis Agents
        self._init_research_agents()

        # Specialized Domain Agents
        self._init_specialized_agents()

    def _init_software_dev_agents(self):
        """Initialize software development agents."""
        agents = [
            ExpertAgent(
                agent_id="python_expert",
                name="Python Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in Python development, from scripts to enterprise applications",
                capabilities=[
                    AgentCapability("code_generation", "Generate Python code", "Create Flask API"),
                    AgentCapability("debugging", "Debug Python issues", "Fix async/await bug"),
                    AgentCapability("optimization", "Optimize Python performance", "Speed up data processing"),
                    AgentCapability("architecture", "Design Python systems", "Microservices architecture"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="javascript_expert",
                name="JavaScript Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in JavaScript/TypeScript, React, Node.js, and web development",
                capabilities=[
                    AgentCapability("frontend", "Build React/Vue/Angular apps", "Create dashboard"),
                    AgentCapability("backend", "Node.js/Express servers", "Build REST API"),
                    AgentCapability("typescript", "TypeScript development", "Add type safety"),
                    AgentCapability("async", "Async programming", "Handle promises/async"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="mobile_dev_expert",
                name="Mobile Development Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in iOS, Android, React Native, and Flutter development",
                capabilities=[
                    AgentCapability("ios", "Swift/SwiftUI development", "Build iOS app"),
                    AgentCapability("android", "Kotlin/Jetpack development", "Build Android app"),
                    AgentCapability("cross_platform", "React Native/Flutter", "Cross-platform app"),
                    AgentCapability("mobile_ui", "Mobile UI/UX patterns", "Design mobile interface"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=600,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="database_expert",
                name="Database Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in SQL, NoSQL, database design, and optimization",
                capabilities=[
                    AgentCapability("sql_design", "Design SQL schemas", "Create normalized schema"),
                    AgentCapability("nosql_design", "Design NoSQL schemas", "MongoDB data model"),
                    AgentCapability("query_optimization", "Optimize queries", "Speed up slow queries"),
                    AgentCapability("migration", "Database migrations", "Migrate MySQL to PostgreSQL"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=450,
                reliability=0.96
            ),

            ExpertAgent(
                agent_id="api_expert",
                name="API Design Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in REST, GraphQL, gRPC, and API design patterns",
                capabilities=[
                    AgentCapability("rest_api", "Design REST APIs", "Create RESTful endpoints"),
                    AgentCapability("graphql", "Design GraphQL APIs", "Build GraphQL schema"),
                    AgentCapability("api_security", "Secure APIs", "Add OAuth2/JWT"),
                    AgentCapability("api_docs", "Document APIs", "Generate OpenAPI spec"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=480,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="ui_ux_coder",
                name="UI/UX Code Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in implementing UI/UX designs with HTML/CSS/JS",
                capabilities=[
                    AgentCapability("responsive_design", "Implement responsive layouts", "Mobile-first design"),
                    AgentCapability("animations", "CSS/JS animations", "Smooth transitions"),
                    AgentCapability("accessibility", "WCAG compliance", "Screen reader support"),
                    AgentCapability("frameworks", "Tailwind/Bootstrap/Material", "Use UI frameworks"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="security_expert",
                name="Security Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in application security, vulnerability assessment, and secure coding",
                capabilities=[
                    AgentCapability("vulnerability_scan", "Scan for vulnerabilities", "Find SQL injection"),
                    AgentCapability("secure_coding", "Write secure code", "Prevent XSS"),
                    AgentCapability("penetration_test", "Pen testing", "Test authentication"),
                    AgentCapability("encryption", "Implement encryption", "Add AES encryption"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=550,
                reliability=0.97
            ),

            ExpertAgent(
                agent_id="testing_expert",
                name="Testing & QA Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in unit testing, integration testing, and test automation",
                capabilities=[
                    AgentCapability("unit_tests", "Write unit tests", "Pytest/Jest tests"),
                    AgentCapability("integration_tests", "Write integration tests", "API test suite"),
                    AgentCapability("e2e_tests", "End-to-end tests", "Selenium/Playwright"),
                    AgentCapability("test_strategy", "Design test strategy", "Test pyramid plan"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="performance_expert",
                name="Performance Optimization Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in profiling, optimization, and performance tuning",
                capabilities=[
                    AgentCapability("profiling", "Profile applications", "Find bottlenecks"),
                    AgentCapability("optimization", "Optimize code", "Reduce time complexity"),
                    AgentCapability("caching", "Implement caching", "Redis/Memcached"),
                    AgentCapability("load_testing", "Load testing", "Stress test system"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="game_dev_expert",
                name="Game Development Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in game development with Unity, Unreal, and game engines",
                capabilities=[
                    AgentCapability("unity", "Unity development", "Create Unity game"),
                    AgentCapability("unreal", "Unreal Engine", "Build Unreal game"),
                    AgentCapability("game_mechanics", "Design game mechanics", "Create gameplay loop"),
                    AgentCapability("game_physics", "Game physics", "Implement collision"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=580,
                reliability=0.91
            ),

            ExpertAgent(
                agent_id="blockchain_expert",
                name="Blockchain Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in blockchain, smart contracts, and Web3 development",
                capabilities=[
                    AgentCapability("smart_contracts", "Write smart contracts", "Solidity contracts"),
                    AgentCapability("web3", "Web3 integration", "Connect to blockchain"),
                    AgentCapability("nft", "NFT development", "Create NFT marketplace"),
                    AgentCapability("defi", "DeFi protocols", "Build DeFi app"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=620,
                reliability=0.89
            ),

            ExpertAgent(
                agent_id="embedded_expert",
                name="Embedded Systems Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in embedded systems, IoT, and hardware programming",
                capabilities=[
                    AgentCapability("microcontroller", "Program microcontrollers", "Arduino/ESP32 code"),
                    AgentCapability("iot", "IoT development", "Build IoT device"),
                    AgentCapability("real_time", "Real-time systems", "RTOS programming"),
                    AgentCapability("hardware_interface", "Hardware interfacing", "I2C/SPI/UART"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="lowcode_expert",
                name="Low-Code/No-Code Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in low-code platforms like Bubble, Webflow, Zapier",
                capabilities=[
                    AgentCapability("bubble", "Build Bubble apps", "Create SaaS app"),
                    AgentCapability("webflow", "Design Webflow sites", "Build website"),
                    AgentCapability("zapier", "Automate with Zapier", "Connect services"),
                    AgentCapability("airtable", "Build Airtable apps", "Create database app"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=450,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="legacy_modernization",
                name="Legacy Code Modernization Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in modernizing legacy codebases and migration",
                capabilities=[
                    AgentCapability("refactoring", "Refactor legacy code", "Clean up tech debt"),
                    AgentCapability("migration", "Migrate to modern stack", "Move to microservices"),
                    AgentCapability("documentation", "Document legacy systems", "Create architecture docs"),
                    AgentCapability("testing_legacy", "Add tests to legacy code", "Test untested code"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=560,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="code_reviewer",
                name="Code Review Expert",
                category=AgentCategory.SOFTWARE_DEV,
                description="Expert in code review, best practices, and code quality",
                capabilities=[
                    AgentCapability("code_review", "Review code quality", "Find issues"),
                    AgentCapability("best_practices", "Enforce best practices", "Check patterns"),
                    AgentCapability("style_guide", "Enforce style guide", "Check formatting"),
                    AgentCapability("suggestions", "Suggest improvements", "Optimization ideas"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=470,
                reliability=0.96
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_data_analytics_agents(self):
        """Initialize data and analytics agents."""
        agents = [
            ExpertAgent(
                agent_id="data_scientist",
                name="Data Science Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in statistical analysis, ML, and data science",
                capabilities=[
                    AgentCapability("eda", "Exploratory data analysis", "Analyze dataset"),
                    AgentCapability("ml_modeling", "Build ML models", "Train classifier"),
                    AgentCapability("feature_engineering", "Feature engineering", "Create features"),
                    AgentCapability("model_evaluation", "Evaluate models", "Compare performance"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=550,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="ml_engineer",
                name="ML Engineering Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in production ML systems, MLOps, and model deployment",
                capabilities=[
                    AgentCapability("model_deployment", "Deploy ML models", "Serve models"),
                    AgentCapability("mlops", "MLOps pipelines", "CI/CD for ML"),
                    AgentCapability("model_monitoring", "Monitor models", "Track drift"),
                    AgentCapability("scaling", "Scale ML systems", "Handle production load"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=580,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="data_engineer",
                name="Data Engineering Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in data pipelines, ETL, and data infrastructure",
                capabilities=[
                    AgentCapability("etl", "Build ETL pipelines", "Extract/transform/load"),
                    AgentCapability("data_warehouse", "Design data warehouses", "Create star schema"),
                    AgentCapability("streaming", "Stream processing", "Kafka/Spark streaming"),
                    AgentCapability("orchestration", "Orchestrate pipelines", "Airflow workflows"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="bi_analyst",
                name="Business Intelligence Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in BI tools, dashboards, and business analytics",
                capabilities=[
                    AgentCapability("dashboards", "Create dashboards", "Tableau/PowerBI"),
                    AgentCapability("reporting", "Build reports", "Automated reports"),
                    AgentCapability("kpi", "Define KPIs", "Track metrics"),
                    AgentCapability("visualization", "Data visualization", "Interactive charts"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="nlp_expert",
                name="NLP Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in natural language processing and text analytics",
                capabilities=[
                    AgentCapability("text_classification", "Classify text", "Sentiment analysis"),
                    AgentCapability("ner", "Named entity recognition", "Extract entities"),
                    AgentCapability("text_generation", "Generate text", "Content creation"),
                    AgentCapability("embeddings", "Text embeddings", "Semantic search"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=600,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="computer_vision",
                name="Computer Vision Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in image processing, object detection, and vision AI",
                capabilities=[
                    AgentCapability("image_classification", "Classify images", "Identify objects"),
                    AgentCapability("object_detection", "Detect objects", "Bounding boxes"),
                    AgentCapability("segmentation", "Image segmentation", "Pixel-level masks"),
                    AgentCapability("ocr", "Optical character recognition", "Extract text"),
                ],
                endpoint="realtime_agent",  # Use embodied AI
                model_type="vision",
                cost_per_call=0.0,
                avg_latency_ms=800,
                reliability=0.91
            ),

            ExpertAgent(
                agent_id="time_series",
                name="Time Series Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in time series forecasting and analysis",
                capabilities=[
                    AgentCapability("forecasting", "Forecast future values", "Predict sales"),
                    AgentCapability("anomaly_detection", "Detect anomalies", "Find outliers"),
                    AgentCapability("seasonality", "Analyze seasonality", "Find patterns"),
                    AgentCapability("trend_analysis", "Analyze trends", "Identify changes"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="recommender",
                name="Recommendation Systems Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in recommendation engines and personalization",
                capabilities=[
                    AgentCapability("collaborative_filtering", "Collaborative filtering", "User-based recs"),
                    AgentCapability("content_based", "Content-based filtering", "Item similarity"),
                    AgentCapability("hybrid", "Hybrid systems", "Combine approaches"),
                    AgentCapability("personalization", "Personalize experience", "Custom feeds"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=560,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="web_scraper",
                name="Web Scraping Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in web scraping and data extraction",
                capabilities=[
                    AgentCapability("scraping", "Scrape websites", "Extract data"),
                    AgentCapability("crawling", "Crawl websites", "Discover pages"),
                    AgentCapability("parsing", "Parse HTML/JSON", "Extract structured data"),
                    AgentCapability("automation", "Automate extraction", "Scheduled scraping"),
                ],
                endpoint="Crawl4AI",  # Use Crawl4AI
                model_type="tool",
                cost_per_call=0.0,
                avg_latency_ms=2000,
                reliability=0.96
            ),

            ExpertAgent(
                agent_id="statistical_analyst",
                name="Statistical Analysis Expert",
                category=AgentCategory.DATA_ANALYTICS,
                description="Expert in statistical testing and experimental design",
                capabilities=[
                    AgentCapability("hypothesis_testing", "Hypothesis testing", "T-tests/ANOVA"),
                    AgentCapability("ab_testing", "A/B testing", "Experiment design"),
                    AgentCapability("regression", "Regression analysis", "Linear/logistic"),
                    AgentCapability("causal_inference", "Causal inference", "Determine causation"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.95
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_creative_design_agents(self):
        """Initialize creative and design agents."""
        agents = [
            ExpertAgent(
                agent_id="graphic_designer",
                name="Graphic Design Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in visual design, branding, and graphic creation",
                capabilities=[
                    AgentCapability("logo_design", "Design logos", "Create brand identity"),
                    AgentCapability("layout", "Layout design", "Magazine/poster layouts"),
                    AgentCapability("typography", "Typography design", "Font pairing"),
                    AgentCapability("color_theory", "Color schemes", "Brand colors"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=550,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="3d_artist",
                name="3D Modeling Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in 3D modeling, animation, and rendering",
                capabilities=[
                    AgentCapability("modeling", "3D modeling", "Create 3D models"),
                    AgentCapability("texturing", "Texture creation", "Apply materials"),
                    AgentCapability("animation", "3D animation", "Animate models"),
                    AgentCapability("rendering", "Rendering", "Create photorealistic renders"),
                ],
                endpoint="visualization",  # Use Manim
                model_type="tool",
                cost_per_call=0.0,
                avg_latency_ms=3000,
                reliability=0.90
            ),

            ExpertAgent(
                agent_id="video_editor",
                name="Video Editing Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in video editing, motion graphics, and production",
                capabilities=[
                    AgentCapability("editing", "Edit videos", "Cut/splice footage"),
                    AgentCapability("motion_graphics", "Motion graphics", "Animated titles"),
                    AgentCapability("color_grading", "Color grading", "Color correction"),
                    AgentCapability("audio_sync", "Audio syncing", "Sync audio/video"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=600,
                reliability=0.91
            ),

            ExpertAgent(
                agent_id="music_composer",
                name="Music Composition Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in music composition, sound design, and audio production",
                capabilities=[
                    AgentCapability("composition", "Compose music", "Create melodies"),
                    AgentCapability("sound_design", "Sound design", "Create sound effects"),
                    AgentCapability("mixing", "Audio mixing", "Mix tracks"),
                    AgentCapability("mastering", "Audio mastering", "Final polish"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=620,
                reliability=0.89
            ),

            ExpertAgent(
                agent_id="writer",
                name="Creative Writing Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in creative writing, storytelling, and content creation",
                capabilities=[
                    AgentCapability("storytelling", "Write stories", "Create narratives"),
                    AgentCapability("copywriting", "Write copy", "Marketing content"),
                    AgentCapability("technical_writing", "Technical writing", "Documentation"),
                    AgentCapability("editing", "Edit content", "Improve clarity"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="photographer",
                name="Photography Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in photography, photo editing, and visual storytelling",
                capabilities=[
                    AgentCapability("composition", "Photo composition", "Frame shots"),
                    AgentCapability("lighting", "Lighting setup", "Light subjects"),
                    AgentCapability("editing", "Photo editing", "Retouch images"),
                    AgentCapability("color_grading", "Color grading", "Stylize photos"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=530,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="animator",
                name="Animation Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in 2D/3D animation and character animation",
                capabilities=[
                    AgentCapability("2d_animation", "2D animation", "Animate sprites"),
                    AgentCapability("character_animation", "Character animation", "Animate characters"),
                    AgentCapability("rigging", "Character rigging", "Create rigs"),
                    AgentCapability("lip_sync", "Lip sync", "Sync mouth to audio"),
                ],
                endpoint="visualization",
                model_type="tool",
                cost_per_call=0.0,
                avg_latency_ms=2500,
                reliability=0.91
            ),

            ExpertAgent(
                agent_id="ui_designer",
                name="UI Design Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in user interface design and interaction design",
                capabilities=[
                    AgentCapability("wireframing", "Create wireframes", "Sketch layouts"),
                    AgentCapability("mockups", "Design mockups", "High-fidelity designs"),
                    AgentCapability("prototyping", "Interactive prototypes", "Clickable prototypes"),
                    AgentCapability("design_systems", "Design systems", "Component libraries"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="ux_researcher",
                name="UX Research Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in user research, usability testing, and UX strategy",
                capabilities=[
                    AgentCapability("user_research", "Conduct user research", "Interviews/surveys"),
                    AgentCapability("usability_testing", "Usability testing", "Test designs"),
                    AgentCapability("persona_creation", "Create personas", "User personas"),
                    AgentCapability("journey_mapping", "Journey mapping", "User journeys"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="brand_strategist",
                name="Brand Strategy Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in branding, brand identity, and brand strategy",
                capabilities=[
                    AgentCapability("brand_identity", "Create brand identity", "Logo/colors/voice"),
                    AgentCapability("brand_positioning", "Brand positioning", "Market position"),
                    AgentCapability("brand_guidelines", "Brand guidelines", "Usage rules"),
                    AgentCapability("rebranding", "Rebranding", "Refresh brand"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="game_designer",
                name="Game Design Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in game design, mechanics, and player experience",
                capabilities=[
                    AgentCapability("game_mechanics", "Design game mechanics", "Core loop"),
                    AgentCapability("level_design", "Level design", "Create levels"),
                    AgentCapability("balancing", "Game balancing", "Balance difficulty"),
                    AgentCapability("narrative_design", "Narrative design", "Story integration"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=560,
                reliability=0.91
            ),

            ExpertAgent(
                agent_id="accessibility_expert",
                name="Accessibility Expert",
                category=AgentCategory.CREATIVE_DESIGN,
                description="Expert in accessibility, inclusive design, and WCAG compliance",
                capabilities=[
                    AgentCapability("wcag_audit", "WCAG compliance audit", "Check accessibility"),
                    AgentCapability("screen_reader", "Screen reader optimization", "Alt text/ARIA"),
                    AgentCapability("color_contrast", "Color contrast", "Ensure readability"),
                    AgentCapability("inclusive_design", "Inclusive design", "Design for all"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.95
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_business_ops_agents(self):
        """Initialize business and operations agents."""
        agents = [
            ExpertAgent(
                agent_id="business_analyst",
                name="Business Analysis Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in business analysis, requirements gathering, and process optimization",
                capabilities=[
                    AgentCapability("requirements", "Gather requirements", "Elicit needs"),
                    AgentCapability("process_mapping", "Map processes", "Document workflows"),
                    AgentCapability("gap_analysis", "Gap analysis", "Identify gaps"),
                    AgentCapability("roi_analysis", "ROI analysis", "Calculate returns"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=480,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="project_manager",
                name="Project Management Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in project management, agile, and team coordination",
                capabilities=[
                    AgentCapability("planning", "Project planning", "Create project plan"),
                    AgentCapability("scheduling", "Task scheduling", "Gantt charts"),
                    AgentCapability("risk_management", "Risk management", "Identify risks"),
                    AgentCapability("agile", "Agile methodologies", "Scrum/Kanban"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="marketing_expert",
                name="Marketing Strategy Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in marketing strategy, campaigns, and growth",
                capabilities=[
                    AgentCapability("strategy", "Marketing strategy", "Growth plan"),
                    AgentCapability("campaigns", "Campaign planning", "Marketing campaigns"),
                    AgentCapability("seo", "SEO optimization", "Search ranking"),
                    AgentCapability("analytics", "Marketing analytics", "Track performance"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="sales_expert",
                name="Sales Strategy Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in sales strategy, pipelines, and conversion optimization",
                capabilities=[
                    AgentCapability("pipeline", "Sales pipeline design", "Lead management"),
                    AgentCapability("conversion", "Conversion optimization", "Improve close rate"),
                    AgentCapability("crm", "CRM strategy", "Customer management"),
                    AgentCapability("forecasting", "Sales forecasting", "Predict revenue"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="finance_expert",
                name="Financial Analysis Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in financial analysis, budgeting, and forecasting",
                capabilities=[
                    AgentCapability("budgeting", "Create budgets", "Budget planning"),
                    AgentCapability("forecasting", "Financial forecasting", "Revenue projections"),
                    AgentCapability("analysis", "Financial analysis", "Analyze statements"),
                    AgentCapability("modeling", "Financial modeling", "Build models"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="hr_expert",
                name="Human Resources Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in HR, recruiting, and people management",
                capabilities=[
                    AgentCapability("recruiting", "Recruiting strategy", "Hire talent"),
                    AgentCapability("onboarding", "Employee onboarding", "New hire process"),
                    AgentCapability("performance", "Performance management", "Reviews/feedback"),
                    AgentCapability("culture", "Culture building", "Company culture"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="legal_expert",
                name="Legal & Compliance Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in legal issues, contracts, and compliance",
                capabilities=[
                    AgentCapability("contracts", "Review contracts", "Draft agreements"),
                    AgentCapability("compliance", "Compliance audit", "GDPR/CCPA"),
                    AgentCapability("intellectual_property", "IP protection", "Patents/trademarks"),
                    AgentCapability("risk_assessment", "Legal risk assessment", "Identify risks"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="operations_expert",
                name="Operations Excellence Expert",
                category=AgentCategory.BUSINESS_OPS,
                description="Expert in operations optimization, supply chain, and logistics",
                capabilities=[
                    AgentCapability("process_optimization", "Optimize processes", "Lean/Six Sigma"),
                    AgentCapability("supply_chain", "Supply chain management", "Logistics"),
                    AgentCapability("inventory", "Inventory management", "Stock optimization"),
                    AgentCapability("quality", "Quality assurance", "QA processes"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.94
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_education_agents(self):
        """Initialize education and learning agents."""
        agents = [
            ExpertAgent(
                agent_id="curriculum_designer",
                name="Curriculum Design Expert",
                category=AgentCategory.EDUCATION,
                description="Expert in curriculum design, learning paths, and instructional design",
                capabilities=[
                    AgentCapability("curriculum", "Design curriculum", "Learning path"),
                    AgentCapability("assessment", "Create assessments", "Tests/quizzes"),
                    AgentCapability("learning_objectives", "Define learning objectives", "Clear goals"),
                    AgentCapability("scaffolding", "Learning scaffolding", "Progressive difficulty"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="tutor",
                name="Personalized Tutoring Expert",
                category=AgentCategory.EDUCATION,
                description="Expert in one-on-one tutoring and personalized learning",
                capabilities=[
                    AgentCapability("tutoring", "Personalized tutoring", "Explain concepts"),
                    AgentCapability("questioning", "Socratic questioning", "Guide discovery"),
                    AgentCapability("feedback", "Provide feedback", "Constructive guidance"),
                    AgentCapability("motivation", "Student motivation", "Encourage learning"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=480,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="elearning_expert",
                name="E-Learning Platform Expert",
                category=AgentCategory.EDUCATION,
                description="Expert in e-learning platforms, LMS, and online courses",
                capabilities=[
                    AgentCapability("lms_design", "Design LMS", "Learning management system"),
                    AgentCapability("course_creation", "Create online courses", "Video/interactive"),
                    AgentCapability("gamification", "Gamify learning", "Points/badges"),
                    AgentCapability("engagement", "Increase engagement", "Interactive elements"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="language_teacher",
                name="Language Teaching Expert",
                category=AgentCategory.EDUCATION,
                description="Expert in language teaching and second language acquisition",
                capabilities=[
                    AgentCapability("language_instruction", "Teach languages", "Grammar/vocab"),
                    AgentCapability("pronunciation", "Pronunciation coaching", "Speaking practice"),
                    AgentCapability("conversation", "Conversation practice", "Dialogue"),
                    AgentCapability("cultural_context", "Cultural context", "Cultural nuances"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="stem_educator",
                name="STEM Education Expert",
                category=AgentCategory.EDUCATION,
                description="Expert in teaching STEM subjects with hands-on activities",
                capabilities=[
                    AgentCapability("math", "Teach mathematics", "Concepts/problem solving"),
                    AgentCapability("science", "Teach science", "Experiments/inquiry"),
                    AgentCapability("coding", "Teach programming", "CS concepts"),
                    AgentCapability("engineering", "Teach engineering", "Design/build"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=530,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="visualization_teacher",
                name="Visual Learning Expert",
                category=AgentCategory.EDUCATION,
                description="Expert in visual explanations and educational animations",
                capabilities=[
                    AgentCapability("visualization", "Create visualizations", "3D animations"),
                    AgentCapability("analogies", "Create analogies", "Relate to known"),
                    AgentCapability("storytelling", "Educational storytelling", "Narrative learning"),
                    AgentCapability("multimodal", "Multimodal learning", "Visual/audio/kinesthetic"),
                ],
                endpoint="visualization",  # Use Manim
                model_type="tool",
                cost_per_call=0.0,
                avg_latency_ms=2000,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="assessment_expert",
                name="Educational Assessment Expert",
                category=AgentCategory.EDUCATION,
                description="Expert in assessment design, evaluation, and learning analytics",
                capabilities=[
                    AgentCapability("assessment_design", "Design assessments", "Tests/projects"),
                    AgentCapability("rubrics", "Create rubrics", "Grading criteria"),
                    AgentCapability("learning_analytics", "Learning analytics", "Track progress"),
                    AgentCapability("adaptive_testing", "Adaptive testing", "Personalized tests"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.93
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_health_agents(self):
        """Initialize health and wellness agents."""
        agents = [
            ExpertAgent(
                agent_id="health_coach",
                name="Health & Wellness Coach",
                category=AgentCategory.HEALTH,
                description="Expert in health coaching, nutrition, and fitness planning",
                capabilities=[
                    AgentCapability("nutrition", "Nutrition planning", "Meal plans"),
                    AgentCapability("fitness", "Fitness planning", "Workout routines"),
                    AgentCapability("habit_building", "Build healthy habits", "Behavior change"),
                    AgentCapability("wellness", "Wellness guidance", "Holistic health"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="mental_health",
                name="Mental Health Support Expert",
                category=AgentCategory.HEALTH,
                description="Expert in mental health support and wellness strategies",
                capabilities=[
                    AgentCapability("cbt", "CBT techniques", "Cognitive reframing"),
                    AgentCapability("mindfulness", "Mindfulness practices", "Meditation"),
                    AgentCapability("stress_management", "Stress management", "Coping strategies"),
                    AgentCapability("emotional_support", "Emotional support", "Active listening"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="medical_info",
                name="Medical Information Expert",
                category=AgentCategory.HEALTH,
                description="Expert in medical information and health education (not diagnosis)",
                capabilities=[
                    AgentCapability("health_info", "Provide health info", "Explain conditions"),
                    AgentCapability("symptom_info", "Symptom information", "When to see doctor"),
                    AgentCapability("medication_info", "Medication information", "Side effects"),
                    AgentCapability("prevention", "Prevention strategies", "Healthy living"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="rehabilitation",
                name="Rehabilitation Expert",
                category=AgentCategory.HEALTH,
                description="Expert in physical rehabilitation and recovery",
                capabilities=[
                    AgentCapability("exercises", "Rehabilitation exercises", "Recovery routines"),
                    AgentCapability("injury_prevention", "Injury prevention", "Safe practices"),
                    AgentCapability("mobility", "Improve mobility", "Range of motion"),
                    AgentCapability("pain_management", "Pain management", "Non-pharmacological"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="sleep_expert",
                name="Sleep Optimization Expert",
                category=AgentCategory.HEALTH,
                description="Expert in sleep optimization and circadian rhythm management",
                capabilities=[
                    AgentCapability("sleep_hygiene", "Sleep hygiene", "Better sleep habits"),
                    AgentCapability("insomnia", "Insomnia strategies", "Fall asleep faster"),
                    AgentCapability("circadian", "Circadian optimization", "Sleep schedule"),
                    AgentCapability("sleep_tracking", "Sleep tracking", "Monitor quality"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=480,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="health_data",
                name="Health Data Analytics Expert",
                category=AgentCategory.HEALTH,
                description="Expert in health data analysis and personal health tracking",
                capabilities=[
                    AgentCapability("tracking", "Track health metrics", "Monitor vitals"),
                    AgentCapability("analysis", "Analyze health data", "Find patterns"),
                    AgentCapability("visualization", "Visualize health data", "Charts/graphs"),
                    AgentCapability("insights", "Generate insights", "Actionable findings"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.94
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_communication_agents(self):
        """Initialize communication and social agents."""
        agents = [
            ExpertAgent(
                agent_id="communication_coach",
                name="Communication Skills Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in communication skills and interpersonal communication",
                capabilities=[
                    AgentCapability("public_speaking", "Public speaking", "Presentation skills"),
                    AgentCapability("active_listening", "Active listening", "Empathetic listening"),
                    AgentCapability("conflict_resolution", "Conflict resolution", "Mediation"),
                    AgentCapability("persuasion", "Persuasive communication", "Influence"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=490,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="social_media",
                name="Social Media Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in social media strategy and content creation",
                capabilities=[
                    AgentCapability("strategy", "Social media strategy", "Platform strategy"),
                    AgentCapability("content", "Content creation", "Posts/stories"),
                    AgentCapability("engagement", "Increase engagement", "Community building"),
                    AgentCapability("analytics", "Social analytics", "Track performance"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="email_expert",
                name="Email Communication Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in professional email writing and email marketing",
                capabilities=[
                    AgentCapability("professional_email", "Professional emails", "Business correspondence"),
                    AgentCapability("email_marketing", "Email marketing", "Campaigns/newsletters"),
                    AgentCapability("templates", "Email templates", "Reusable templates"),
                    AgentCapability("tone", "Email tone", "Appropriate tone"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=470,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="presentation_expert",
                name="Presentation Design Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in presentation design and delivery",
                capabilities=[
                    AgentCapability("slide_design", "Design slides", "Visual presentations"),
                    AgentCapability("storytelling", "Presentation storytelling", "Narrative structure"),
                    AgentCapability("delivery", "Presentation delivery", "Speaking techniques"),
                    AgentCapability("data_viz", "Data visualization", "Charts in presentations"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="chatbot_builder",
                name="Conversational AI Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in building chatbots and conversational interfaces",
                capabilities=[
                    AgentCapability("dialog_design", "Design conversations", "Dialog flows"),
                    AgentCapability("intent_recognition", "Intent recognition", "Understand user intent"),
                    AgentCapability("personality", "Bot personality", "Voice/tone"),
                    AgentCapability("integration", "Integrate chatbots", "Deploy to platforms"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=530,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="translation_expert",
                name="Translation Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in translation and localization",
                capabilities=[
                    AgentCapability("translation", "Translate text", "Multiple languages"),
                    AgentCapability("localization", "Localize content", "Cultural adaptation"),
                    AgentCapability("interpretation", "Interpret meaning", "Context-aware"),
                    AgentCapability("quality", "Translation quality", "Natural phrasing"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="community_manager",
                name="Community Management Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in community building and moderation",
                capabilities=[
                    AgentCapability("community_building", "Build communities", "Grow engagement"),
                    AgentCapability("moderation", "Moderate discussions", "Handle conflicts"),
                    AgentCapability("events", "Organize events", "Virtual/in-person"),
                    AgentCapability("metrics", "Community metrics", "Track health"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="pr_expert",
                name="Public Relations Expert",
                category=AgentCategory.COMMUNICATION,
                description="Expert in public relations and media communications",
                capabilities=[
                    AgentCapability("press_releases", "Write press releases", "Media announcements"),
                    AgentCapability("media_relations", "Media relations", "Journalist outreach"),
                    AgentCapability("crisis_management", "Crisis communication", "Handle PR crises"),
                    AgentCapability("messaging", "Messaging strategy", "Consistent messaging"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.93
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_infrastructure_agents(self):
        """Initialize infrastructure and DevOps agents."""
        agents = [
            ExpertAgent(
                agent_id="devops_engineer",
                name="DevOps Engineering Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in DevOps practices, CI/CD, and automation",
                capabilities=[
                    AgentCapability("ci_cd", "CI/CD pipelines", "Automate deployment"),
                    AgentCapability("infrastructure_as_code", "Infrastructure as code", "Terraform/CloudFormation"),
                    AgentCapability("monitoring", "System monitoring", "Prometheus/Grafana"),
                    AgentCapability("automation", "DevOps automation", "Ansible/Chef"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="cloud_architect",
                name="Cloud Architecture Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in cloud architecture on AWS, Azure, GCP",
                capabilities=[
                    AgentCapability("aws", "AWS architecture", "Design AWS solutions"),
                    AgentCapability("azure", "Azure architecture", "Design Azure solutions"),
                    AgentCapability("gcp", "GCP architecture", "Design GCP solutions"),
                    AgentCapability("multi_cloud", "Multi-cloud strategy", "Cross-cloud"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=560,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="kubernetes_expert",
                name="Kubernetes Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in Kubernetes orchestration and container management",
                capabilities=[
                    AgentCapability("k8s_deployment", "Deploy on Kubernetes", "Deployments/services"),
                    AgentCapability("helm", "Helm charts", "Package applications"),
                    AgentCapability("scaling", "Auto-scaling", "HPA/VPA"),
                    AgentCapability("monitoring", "K8s monitoring", "Prometheus/metrics"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=580,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="docker_expert",
                name="Docker & Containerization Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in Docker, containerization, and container optimization",
                capabilities=[
                    AgentCapability("dockerfile", "Write Dockerfiles", "Container images"),
                    AgentCapability("compose", "Docker Compose", "Multi-container apps"),
                    AgentCapability("optimization", "Optimize containers", "Reduce size"),
                    AgentCapability("registry", "Container registries", "Push/pull images"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="network_expert",
                name="Network Engineering Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in networking, network security, and configuration",
                capabilities=[
                    AgentCapability("network_design", "Network design", "Topology design"),
                    AgentCapability("security", "Network security", "Firewalls/VPNs"),
                    AgentCapability("troubleshooting", "Network troubleshooting", "Diagnose issues"),
                    AgentCapability("load_balancing", "Load balancing", "Distribute traffic"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="database_admin",
                name="Database Administration Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in database administration and performance tuning",
                capabilities=[
                    AgentCapability("backup", "Database backup", "Backup/restore"),
                    AgentCapability("replication", "Database replication", "High availability"),
                    AgentCapability("tuning", "Performance tuning", "Optimize queries"),
                    AgentCapability("monitoring", "Database monitoring", "Track performance"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=530,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="sre_expert",
                name="Site Reliability Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in SRE practices, reliability, and incident management",
                capabilities=[
                    AgentCapability("slo_sli", "Define SLOs/SLIs", "Reliability metrics"),
                    AgentCapability("incident_response", "Incident response", "Handle outages"),
                    AgentCapability("postmortems", "Write postmortems", "Learn from incidents"),
                    AgentCapability("on_call", "On-call practices", "Alerting/escalation"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="serverless_expert",
                name="Serverless Architecture Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in serverless architectures and FaaS platforms",
                capabilities=[
                    AgentCapability("lambda", "AWS Lambda functions", "Serverless compute"),
                    AgentCapability("functions", "Cloud Functions", "GCP/Azure functions"),
                    AgentCapability("event_driven", "Event-driven architecture", "Event sourcing"),
                    AgentCapability("cost_optimization", "Serverless cost optimization", "Reduce costs"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=550,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="backup_recovery",
                name="Backup & Recovery Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in backup strategies and disaster recovery",
                capabilities=[
                    AgentCapability("backup_strategy", "Backup strategy", "3-2-1 rule"),
                    AgentCapability("disaster_recovery", "Disaster recovery", "DR planning"),
                    AgentCapability("rto_rpo", "RTO/RPO planning", "Recovery objectives"),
                    AgentCapability("testing", "Test backups", "Verify recoverability"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="cost_optimizer",
                name="Cloud Cost Optimization Expert",
                category=AgentCategory.INFRASTRUCTURE,
                description="Expert in cloud cost optimization and FinOps",
                capabilities=[
                    AgentCapability("cost_analysis", "Analyze cloud costs", "Find waste"),
                    AgentCapability("rightsizing", "Rightsize resources", "Optimize sizing"),
                    AgentCapability("reserved_instances", "Reserved instances", "Savings plans"),
                    AgentCapability("tagging", "Cost tagging", "Track expenses"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.93
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_research_agents(self):
        """Initialize research and analysis agents."""
        agents = [
            ExpertAgent(
                agent_id="academic_researcher",
                name="Academic Research Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in academic research methodology and literature review",
                capabilities=[
                    AgentCapability("literature_review", "Literature review", "Survey papers"),
                    AgentCapability("research_design", "Research design", "Methodology"),
                    AgentCapability("data_collection", "Data collection", "Research methods"),
                    AgentCapability("analysis", "Data analysis", "Statistical analysis"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="market_researcher",
                name="Market Research Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in market research and competitive analysis",
                capabilities=[
                    AgentCapability("market_analysis", "Market analysis", "Market sizing"),
                    AgentCapability("competitive_analysis", "Competitive analysis", "Competitor research"),
                    AgentCapability("customer_research", "Customer research", "Surveys/interviews"),
                    AgentCapability("trends", "Trend analysis", "Market trends"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="patent_researcher",
                name="Patent Research Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in patent research and IP analysis",
                capabilities=[
                    AgentCapability("patent_search", "Patent search", "Find prior art"),
                    AgentCapability("patentability", "Patentability analysis", "Assess novelty"),
                    AgentCapability("infringement", "Infringement analysis", "Check violations"),
                    AgentCapability("landscape", "Patent landscape", "Technology analysis"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=560,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="fact_checker",
                name="Fact Checking Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in fact checking and verification",
                capabilities=[
                    AgentCapability("verification", "Verify claims", "Check facts"),
                    AgentCapability("sources", "Evaluate sources", "Credibility assessment"),
                    AgentCapability("debunking", "Debunk misinformation", "Counter false claims"),
                    AgentCapability("citations", "Check citations", "Verify references"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="data_miner",
                name="Data Mining Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in data mining and knowledge discovery",
                capabilities=[
                    AgentCapability("pattern_discovery", "Discover patterns", "Find insights"),
                    AgentCapability("clustering", "Clustering analysis", "Group similar items"),
                    AgentCapability("association_rules", "Association rules", "Find relationships"),
                    AgentCapability("anomaly_detection", "Detect anomalies", "Find outliers"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=550,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="synthesis_expert",
                name="Knowledge Synthesis Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in synthesizing information from multiple sources",
                capabilities=[
                    AgentCapability("synthesis", "Synthesize information", "Combine sources"),
                    AgentCapability("summarization", "Summarize research", "Key findings"),
                    AgentCapability("comparison", "Compare approaches", "Contrast methods"),
                    AgentCapability("integration", "Integrate knowledge", "Unified view"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=530,
                reliability=0.94
            ),

            ExpertAgent(
                agent_id="hypothesis_generator",
                name="Hypothesis Generation Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in generating research hypotheses and questions",
                capabilities=[
                    AgentCapability("hypothesis_generation", "Generate hypotheses", "Research questions"),
                    AgentCapability("exploration", "Exploratory research", "Open-ended inquiry"),
                    AgentCapability("creativity", "Creative thinking", "Novel connections"),
                    AgentCapability("validation", "Validate hypotheses", "Test assumptions"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="experiment_designer",
                name="Experiment Design Expert",
                category=AgentCategory.RESEARCH,
                description="Expert in designing scientific experiments",
                capabilities=[
                    AgentCapability("experimental_design", "Design experiments", "Control variables"),
                    AgentCapability("ab_testing", "A/B test design", "Randomization"),
                    AgentCapability("sample_size", "Determine sample size", "Power analysis"),
                    AgentCapability("analysis_plan", "Analysis plan", "Statistical methods"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.93
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def _init_specialized_agents(self):
        """Initialize specialized domain agents."""
        agents = [
            ExpertAgent(
                agent_id="agriculture_expert",
                name="Agriculture & Farming Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in agriculture, farming practices, and crop management",
                capabilities=[
                    AgentCapability("crop_planning", "Crop planning", "Rotation/selection"),
                    AgentCapability("soil_management", "Soil management", "Fertility/pH"),
                    AgentCapability("pest_control", "Pest control", "Integrated pest management"),
                    AgentCapability("irrigation", "Irrigation", "Water management"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="sustainability_expert",
                name="Sustainability Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in sustainability, environmental impact, and green practices",
                capabilities=[
                    AgentCapability("carbon_footprint", "Calculate carbon footprint", "Emissions"),
                    AgentCapability("renewable_energy", "Renewable energy", "Solar/wind"),
                    AgentCapability("circular_economy", "Circular economy", "Waste reduction"),
                    AgentCapability("esg", "ESG compliance", "Sustainability reporting"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=530,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="real_estate",
                name="Real Estate Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in real estate, property management, and investment",
                capabilities=[
                    AgentCapability("valuation", "Property valuation", "Appraisals"),
                    AgentCapability("investment_analysis", "Investment analysis", "ROI calculation"),
                    AgentCapability("property_management", "Property management", "Tenant management"),
                    AgentCapability("market_trends", "Real estate trends", "Market analysis"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="automotive_expert",
                name="Automotive Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in automotive technology and vehicle systems",
                capabilities=[
                    AgentCapability("diagnostics", "Vehicle diagnostics", "Troubleshoot issues"),
                    AgentCapability("maintenance", "Maintenance planning", "Service schedules"),
                    AgentCapability("ev", "Electric vehicles", "EV technology"),
                    AgentCapability("autonomous", "Autonomous vehicles", "Self-driving tech"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=540,
                reliability=0.91
            ),

            ExpertAgent(
                agent_id="manufacturing_expert",
                name="Manufacturing Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in manufacturing processes and industrial engineering",
                capabilities=[
                    AgentCapability("process_optimization", "Optimize processes", "Lean manufacturing"),
                    AgentCapability("quality_control", "Quality control", "QA/QC"),
                    AgentCapability("automation", "Manufacturing automation", "Robotics"),
                    AgentCapability("supply_chain", "Supply chain", "Procurement/logistics"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=530,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="retail_expert",
                name="Retail & E-commerce Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in retail operations and e-commerce",
                capabilities=[
                    AgentCapability("merchandising", "Merchandising", "Product placement"),
                    AgentCapability("inventory", "Inventory management", "Stock optimization"),
                    AgentCapability("ecommerce", "E-commerce", "Online store"),
                    AgentCapability("customer_experience", "Customer experience", "CX optimization"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="hospitality_expert",
                name="Hospitality Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in hospitality, hotels, and food service",
                capabilities=[
                    AgentCapability("hotel_management", "Hotel management", "Operations"),
                    AgentCapability("food_service", "Food service", "Restaurant operations"),
                    AgentCapability("guest_experience", "Guest experience", "Service quality"),
                    AgentCapability("menu_planning", "Menu planning", "Recipe development"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="event_planner",
                name="Event Planning Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in event planning and coordination",
                capabilities=[
                    AgentCapability("planning", "Event planning", "Logistics/timeline"),
                    AgentCapability("venue", "Venue selection", "Location scouting"),
                    AgentCapability("coordination", "Vendor coordination", "Manage vendors"),
                    AgentCapability("budget", "Budget management", "Cost control"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="nonprofit_expert",
                name="Nonprofit Management Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in nonprofit operations and fundraising",
                capabilities=[
                    AgentCapability("fundraising", "Fundraising", "Donor management"),
                    AgentCapability("grant_writing", "Grant writing", "Proposal writing"),
                    AgentCapability("volunteer", "Volunteer management", "Recruit/retain"),
                    AgentCapability("impact", "Impact measurement", "Track outcomes"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=520,
                reliability=0.93
            ),

            ExpertAgent(
                agent_id="personal_assistant",
                name="Personal Productivity Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in personal productivity and life management",
                capabilities=[
                    AgentCapability("time_management", "Time management", "Schedule optimization"),
                    AgentCapability("task_management", "Task management", "To-do systems"),
                    AgentCapability("goal_setting", "Goal setting", "SMART goals"),
                    AgentCapability("habit_tracking", "Habit tracking", "Build good habits"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=480,
                reliability=0.95
            ),

            ExpertAgent(
                agent_id="travel_expert",
                name="Travel Planning Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in travel planning and itinerary design",
                capabilities=[
                    AgentCapability("itinerary", "Create itineraries", "Day-by-day plans"),
                    AgentCapability("booking", "Booking assistance", "Flights/hotels"),
                    AgentCapability("recommendations", "Destination recommendations", "Activities"),
                    AgentCapability("budget", "Travel budgeting", "Cost estimation"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=510,
                reliability=0.92
            ),

            ExpertAgent(
                agent_id="parenting_expert",
                name="Parenting Support Expert",
                category=AgentCategory.SPECIALIZED,
                description="Expert in parenting strategies and child development",
                capabilities=[
                    AgentCapability("development", "Child development", "Milestones"),
                    AgentCapability("discipline", "Positive discipline", "Behavior management"),
                    AgentCapability("activities", "Educational activities", "Age-appropriate"),
                    AgentCapability("health", "Child health", "Nutrition/safety"),
                ],
                endpoint="http://localhost:8000/v1/chat/completions",
                model_type="llm",
                cost_per_call=0.0,
                avg_latency_ms=500,
                reliability=0.93
            ),
        ]

        for agent in agents:
            self.agents[agent.agent_id] = agent

    def get_agent(self, agent_id: str) -> Optional[ExpertAgent]:
        """Get agent by ID."""
        return self.agents.get(agent_id)

    def get_agents_by_category(self, category: AgentCategory) -> List[ExpertAgent]:
        """Get all agents in a category."""
        return [agent for agent in self.agents.values() if agent.category == category]

    def search_agents(self, query: str) -> List[ExpertAgent]:
        """Search agents by capability or description."""
        query_lower = query.lower()
        results = []

        for agent in self.agents.values():
            # Search in description
            if query_lower in agent.description.lower():
                results.append(agent)
                continue

            # Search in capabilities
            for cap in agent.capabilities:
                if query_lower in cap.name.lower() or query_lower in cap.description.lower():
                    results.append(agent)
                    break

        return results

    def recommend_agents(self, dream_statement: str, dream_category: str) -> List[ExpertAgent]:
        """Recommend agents based on dream statement."""
        # Simple keyword-based recommendation
        # In production, use LLM to understand dream and match to agents

        dream_lower = dream_statement.lower()
        recommended = []

        # Score each agent
        scores = {}
        for agent_id, agent in self.agents.items():
            score = 0

            # Category match
            if dream_category and dream_category.lower() in agent.category.value:
                score += 10

            # Keyword match in capabilities
            for cap in agent.capabilities:
                cap_keywords = cap.name.lower().split('_')
                for keyword in cap_keywords:
                    if keyword in dream_lower and len(keyword) > 3:
                        score += 2

            if score > 0:
                scores[agent_id] = score

        # Sort by score
        sorted_agents = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Return top agents
        return [self.agents[agent_id] for agent_id, score in sorted_agents[:10]]

    def get_stats(self) -> Dict:
        """Get registry statistics."""
        by_category = {}
        for category in AgentCategory:
            by_category[category.value] = len(self.get_agents_by_category(category))

        return {
            'total_agents': len(self.agents),
            'by_category': by_category,
            'avg_latency_ms': sum(a.avg_latency_ms for a in self.agents.values()) / len(self.agents),
            'avg_reliability': sum(a.reliability for a in self.agents.values()) / len(self.agents)
        }


# Example usage
if __name__ == "__main__":
    registry = ExpertAgentRegistry()

    print("=== Expert Agent Registry ===")
    print(f"Total agents: {len(registry.agents)}")
    print()

    # Show stats by category
    stats = registry.get_stats()
    print("Agents by category:")
    for category, count in stats['by_category'].items():
        print(f"  {category}: {count}")
    print()

    # Search example
    print("=== Search: 'python' ===")
    results = registry.search_agents("python")
    for agent in results[:3]:
        print(f"  {agent.name}: {agent.description}")
    print()

    # Recommendation example
    print("=== Recommend for: 'I want to build a mobile app' ===")
    recommended = registry.recommend_agents("I want to build a mobile app", "software")
    for agent in recommended[:5]:
        print(f"  {agent.name}: {agent.description}")
