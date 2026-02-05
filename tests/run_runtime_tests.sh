#!/bin/bash
# Runtime Test Runner
# Manages Docker containers and runs end-to-end tests

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Runtime End-to-End Test Runner${NC}"
echo -e "${BLUE}========================================${NC}\n"

# Check if LLM server is running
echo -e "${YELLOW}Checking prerequisites...${NC}"

if ! curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo -e "${RED}✗ LLM server not running on localhost:8000${NC}"
    echo -e "${YELLOW}Please start the LLM server first:${NC}"
    echo -e "  python -m vllm.entrypoints.openai.api_server \\"
    echo -e "    --model Qwen/Qwen3-VL-2B-Instruct \\"
    echo -e "    --port 8000"
    exit 1
fi
echo -e "${GREEN}✓ LLM server is running${NC}"

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}✗ Docker is not running${NC}"
    echo -e "${YELLOW}Please start Docker Desktop${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker is running${NC}"

# Parse command line arguments
TEST_PATTERN="${1:-tests/runtime_tests/}"
VERBOSE="${2:-}"

echo -e "\n${BLUE}Starting test environment...${NC}"

# Stop any existing containers
echo -e "${YELLOW}Cleaning up existing containers...${NC}"
docker-compose -f docker-compose.test.yml down --volumes --remove-orphans 2>/dev/null || true

# Build and start containers
echo -e "${YELLOW}Building containers...${NC}"
docker-compose -f docker-compose.test.yml build

echo -e "${YELLOW}Starting services...${NC}"
docker-compose -f docker-compose.test.yml up -d redis mock-crossbar mock-apis

# Wait for services to be healthy
echo -e "${YELLOW}Waiting for services to be ready...${NC}"
for i in {1..30}; do
    if docker-compose -f docker-compose.test.yml ps | grep -q "healthy"; then
        break
    fi
    echo -n "."
    sleep 2
done
echo ""

# Check if services are healthy
if ! docker-compose -f docker-compose.test.yml ps | grep -q "healthy"; then
    echo -e "${RED}✗ Services failed to start${NC}"
    docker-compose -f docker-compose.test.yml logs
    docker-compose -f docker-compose.test.yml down
    exit 1
fi

echo -e "${GREEN}✓ All services are ready${NC}\n"

# Run tests
echo -e "${BLUE}Running tests...${NC}"
echo -e "${YELLOW}Test pattern: ${TEST_PATTERN}${NC}\n"

if [ -n "$VERBOSE" ]; then
    docker-compose -f docker-compose.test.yml run --rm app \
        pytest $TEST_PATTERN -v --tb=long --maxfail=5 --timeout=300
else
    docker-compose -f docker-compose.test.yml run --rm app \
        pytest $TEST_PATTERN -v --tb=short --maxfail=5 --timeout=300
fi

TEST_EXIT_CODE=$?

# Cleanup
echo -e "\n${YELLOW}Cleaning up...${NC}"
docker-compose -f docker-compose.test.yml down --volumes

if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo -e "\n${GREEN}========================================${NC}"
    echo -e "${GREEN}✓ All tests passed!${NC}"
    echo -e "${GREEN}========================================${NC}"
else
    echo -e "\n${RED}========================================${NC}"
    echo -e "${RED}✗ Some tests failed${NC}"
    echo -e "${RED}========================================${NC}"
fi

exit $TEST_EXIT_CODE
