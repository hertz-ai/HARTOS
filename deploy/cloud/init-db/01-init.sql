-- =============================================================================
-- HARTOS Database Initialization
-- =============================================================================
-- This script runs on first PostgreSQL container startup
-- =============================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- Core Tables
-- =============================================================================

-- Sessions table for user sessions
CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id VARCHAR(255) UNIQUE NOT NULL,
    user_id VARCHAR(255),
    data JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_sessions_session_id ON sessions(session_id);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);

-- Conversations table for chat history
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    messages JSONB DEFAULT '[]',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_conversations_session_id ON conversations(session_id);
CREATE INDEX idx_conversations_user_id ON conversations(user_id);
CREATE INDEX idx_conversations_created_at ON conversations(created_at);

-- Agents table for agent configurations
CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    config JSONB DEFAULT '{}',
    capabilities JSONB DEFAULT '[]',
    status VARCHAR(50) DEFAULT 'active',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_agents_agent_id ON agents(agent_id);
CREATE INDEX idx_agents_status ON agents(status);

-- =============================================================================
-- Social Features Tables
-- =============================================================================

-- Users table for social features
CREATE TABLE IF NOT EXISTS social_users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(100),
    display_name VARCHAR(255),
    email VARCHAR(255),
    profile_data JSONB DEFAULT '{}',
    karma_points INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_social_users_user_id ON social_users(user_id);
CREATE INDEX idx_social_users_username ON social_users(username);

-- Agent relationships (following, connections)
CREATE TABLE IF NOT EXISTS agent_relationships (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_agent_id VARCHAR(255) NOT NULL,
    target_agent_id VARCHAR(255) NOT NULL,
    relationship_type VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_agent_id, target_agent_id, relationship_type)
);

CREATE INDEX idx_agent_rel_source ON agent_relationships(source_agent_id);
CREATE INDEX idx_agent_rel_target ON agent_relationships(target_agent_id);
CREATE INDEX idx_agent_rel_type ON agent_relationships(relationship_type);

-- Agent activities feed
CREATE TABLE IF NOT EXISTS agent_activities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id VARCHAR(255) NOT NULL,
    activity_type VARCHAR(100) NOT NULL,
    content JSONB DEFAULT '{}',
    visibility VARCHAR(50) DEFAULT 'public',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_activities_agent_id ON agent_activities(agent_id);
CREATE INDEX idx_activities_type ON agent_activities(activity_type);
CREATE INDEX idx_activities_created_at ON agent_activities(created_at);

-- =============================================================================
-- Gamification Tables
-- =============================================================================

-- Achievements
CREATE TABLE IF NOT EXISTS achievements (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code VARCHAR(100) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    points INTEGER DEFAULT 0,
    badge_icon VARCHAR(255),
    requirements JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- User achievements
CREATE TABLE IF NOT EXISTS user_achievements (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) NOT NULL,
    achievement_id UUID REFERENCES achievements(id),
    unlocked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, achievement_id)
);

CREATE INDEX idx_user_achievements_user_id ON user_achievements(user_id);

-- =============================================================================
-- Functions and Triggers
-- =============================================================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply updated_at trigger to tables
CREATE TRIGGER update_sessions_updated_at
    BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_agents_updated_at
    BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_social_users_updated_at
    BEFORE UPDATE ON social_users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- =============================================================================
-- Seed Data
-- =============================================================================

-- Insert default achievements
INSERT INTO achievements (code, name, description, points) VALUES
    ('first_chat', 'First Conversation', 'Started your first conversation with an agent', 10),
    ('power_user', 'Power User', 'Completed 100 conversations', 100),
    ('social_butterfly', 'Social Butterfly', 'Connected with 10 other agents', 50),
    ('helper', 'Helpful Agent', 'Received 50 positive ratings', 75),
    ('explorer', 'Explorer', 'Used 5 different agent capabilities', 25)
ON CONFLICT (code) DO NOTHING;

-- Grant permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO chatbot;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO chatbot;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO chatbot;

-- Log completion
DO $$
BEGIN
    RAISE NOTICE 'Database initialization completed successfully!';
END $$;
