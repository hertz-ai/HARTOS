Professional Coding Agent Requirements

Agent Name: ProfessionalCodingAgent

Description: A comprehensive software development agent that follows complete SDLC from requirements gathering to deployment. Uses nested task ledger with deterministic auto-resume and event-driven architecture.

Capabilities:

1. Repository Management
   - Fork branches and setup repository
   - Validate existing code functionality
   - Review codebase architecture and patterns

2. Design & Specification
   - Create detailed design specifications
   - Identify appropriate design patterns (Factory, Observer, Strategy, Repository, etc.)
   - Search for reusable libraries from Google, org repos, PyPI
   - Avoid reinventing the wheel
   - Plan cross-cutting concerns (AOP, API Gateway)

3. Implementation
   - Implement core functionality with clean code
   - Apply proper design patterns
   - Handle cross-cutting concerns (logging, auth, rate limiting, caching)
   - Use decorators for AOP
   - Configure API Gateway for middleware

4. Comprehensive Testing
   - Write and run unit tests (aim for >90% coverage)
   - Write and run integration tests
   - Write and run functional tests (user scenarios)
   - Run non-functional tests (performance, load, stress testing)
   - Validate all test types pass before proceeding

5. Code Quality & Refactoring
   - Detect code smells (Long Method, Duplicate Code, Large Class)
   - Fix identified smells
   - Refactor for reusability
   - Ensure design patterns properly applied
   - Target code quality score >95

6. Security Scanning
   - Run OWASP security scans
   - Run Veracode scans (when applicable)
   - Generate vulnerability reports
   - Fix all critical and high severity issues
   - Document medium/low issues with remediation plans

7. UI/UX Validation (using VLM Agent)
   - Visual inspection of UI using execute_windows_or_android_command
   - Validate UX flows and user journeys
   - Check accessibility (WCAG AA compliance)
   - Screen reader compatibility
   - Keyboard navigation
   - Fix any visual or UX issues found

8. Documentation
   - Create smart consolidated documentation (single source of truth)
   - Avoid duplicate documentation
   - Update API documentation with Swagger
   - Include code examples
   - Document design decisions

9. Publishing & Deployment
   - Create detailed pull request
   - Include test results, security scan results
   - Document breaking changes
   - Notify team
   - Deploy feature

Technical Requirements:

- Uses nested task ledger (parent-child, siblings, sequential tasks)
- Deterministic auto-resume when dependencies complete
- Event-driven architecture (observes ledger events)
- No LLM intelligence in ledger (all intelligence in agent)
- Reuses existing libraries and patterns
- Follows professional software development standards

Workflow Structure:

Phase 1: Setup & Validation (Sequential)
  - Fork branch
  - Validate existing functionality
  - Review codebase

Phase 2: Design (Sequential)
  - Create design spec
  - Identify patterns
  - Search libraries
  - Plan cross-cutting concerns

Phase 3: Implementation (Sequential)
  - Implement core
  - Apply patterns
  - Handle cross-cutting

Phase 4: Testing (Parallel)
  - Unit tests
  - Integration tests
  - Functional tests
  - Non-functional tests

Phase 5: Quality & Security (Parallel)
  - Code smell detection
  - OWASP scan
  - Veracode scan
  - Vulnerability report

Phase 6: UI/UX (Sequential with VLM)
  - Visual inspection
  - UX flow validation
  - Accessibility check

Phase 7: Documentation & Publishing (Sequential)
  - Smart documentation
  - API docs
  - Create PR
  - Publish feature

Expected Behavior:

- When Phase 1 completes → Phase 2 auto-resumes
- When Phase 2 completes → Phase 3 auto-resumes
- When Phase 3 completes → Phase 4 tasks (all 4 test types) start in parallel
- When all Phase 4 tests complete → Phase 5 tasks (4 scans) start in parallel
- When all Phase 5 scans complete → Phase 6 auto-resumes
- When Phase 6 completes → Phase 7 auto-resumes
- Agent observes events and executes tasks as they become ready
- All task results passed to dependent tasks via messages
