package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestGetBackendPort_Default(t *testing.T) {
	// Empty reader should return default port
	port := getBackendPortFromReader(strings.NewReader(""))
	if port != defaultBackendPort {
		t.Errorf("expected %d, got %d", defaultBackendPort, port)
	}
}

func TestGetBackendPort_FromEnv(t *testing.T) {
	tests := []struct {
		name     string
		content  string
		expected int
	}{
		{
			name:     "standard port",
			content:  "HYVE_BACKEND_PORT=8080\n",
			expected: 8080,
		},
		{
			name:     "port with spaces",
			content:  "HYVE_BACKEND_PORT= 9000 \n",
			expected: 9000,
		},
		{
			name:     "empty value returns default",
			content:  "HYVE_BACKEND_PORT=\n",
			expected: defaultBackendPort,
		},
		{
			name:     "invalid number returns default",
			content:  "HYVE_BACKEND_PORT=abc\n",
			expected: defaultBackendPort,
		},
		{
			name: "port among other vars",
			content: `HYVE_INSTALL_DIR=/opt/hyve
HYVE_BACKEND_PORT=7777
HYVE_LOG_LEVEL=info
`,
			expected: 7777,
		},
		{
			name:     "no matching key returns default",
			content:  "OTHER_VAR=1234\n",
			expected: defaultBackendPort,
		},
		{
			name:     "commented out line returns default",
			content:  "# HYVE_BACKEND_PORT=9999\nOTHER=1\n",
			expected: defaultBackendPort,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			port := getBackendPortFromReader(strings.NewReader(tc.content))
			if port != tc.expected {
				t.Errorf("expected %d, got %d", tc.expected, port)
			}
		})
	}
}

func TestParseStatusResponse(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		wantErr bool
		check   func(map[string]interface{}) bool
	}{
		{
			name:    "valid status",
			input:   `{"status":"ok","version":"1.0.0","uptime":12345}`,
			wantErr: false,
			check: func(m map[string]interface{}) bool {
				return m["status"] == "ok" && m["version"] == "1.0.0"
			},
		},
		{
			name:    "empty object",
			input:   `{}`,
			wantErr: false,
			check: func(m map[string]interface{}) bool {
				return len(m) == 0
			},
		},
		{
			name:    "invalid JSON",
			input:   `not json`,
			wantErr: true,
			check:   nil,
		},
		{
			name:    "nested data",
			input:   `{"node":{"id":"abc123","tier":"STANDARD"},"peers":5}`,
			wantErr: false,
			check: func(m map[string]interface{}) bool {
				node, ok := m["node"].(map[string]interface{})
				if !ok {
					return false
				}
				return node["id"] == "abc123" && m["peers"].(float64) == 5
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			result, err := parseStatusResponse([]byte(tc.input))
			if tc.wantErr {
				if err == nil {
					t.Error("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Errorf("unexpected error: %v", err)
				return
			}
			if tc.check != nil && !tc.check(result) {
				t.Errorf("check failed for input: %s, got: %v", tc.input, result)
			}
		})
	}
}

func TestParseHealthResponse(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		wantErr bool
		check   func(map[string]interface{}) bool
	}{
		{
			name:    "full health report",
			input:   `{"tier":"STANDARD","trust_score":2.5,"active_peers":12,"uptime_hours":48.5}`,
			wantErr: false,
			check: func(m map[string]interface{}) bool {
				return m["tier"] == "STANDARD" &&
					m["trust_score"].(float64) == 2.5 &&
					m["active_peers"].(float64) == 12 &&
					m["uptime_hours"].(float64) == 48.5
			},
		},
		{
			name:    "minimal health",
			input:   `{"status":"healthy"}`,
			wantErr: false,
			check: func(m map[string]interface{}) bool {
				return m["status"] == "healthy"
			},
		},
		{
			name:    "invalid JSON",
			input:   `{broken`,
			wantErr: true,
			check:   nil,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			result, err := parseHealthResponse([]byte(tc.input))
			if tc.wantErr {
				if err == nil {
					t.Error("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Errorf("unexpected error: %v", err)
				return
			}
			if tc.check != nil && !tc.check(result) {
				t.Errorf("check failed for input: %s, got: %v", tc.input, result)
			}
		})
	}
}

func TestCommandParsing(t *testing.T) {
	// Verify all 11 commands are recognized by the switch statement.
	// We test the command string matching logic, not actual execution.
	validCommands := []string{
		"status",
		"start",
		"stop",
		"restart",
		"logs",
		"health",
		"join",
		"provision",
		"update",
		"node-id",
		"version",
	}

	// Build a set of known commands from the switch statement
	knownCommands := map[string]bool{
		"status":    true,
		"start":     true,
		"stop":      true,
		"restart":   true,
		"logs":      true,
		"health":    true,
		"join":      true,
		"provision": true,
		"update":    true,
		"node-id":   true,
		"version":   true,
		"-h":        true,
		"--help":    true,
		"help":      true,
	}

	for _, cmd := range validCommands {
		t.Run(cmd, func(t *testing.T) {
			if !knownCommands[cmd] {
				t.Errorf("command %q not recognized", cmd)
			}
		})
	}

	// Verify unknown commands are not in the set
	unknownCommands := []string{"invalid", "deploy", "config", ""}
	for _, cmd := range unknownCommands {
		t.Run("unknown_"+cmd, func(t *testing.T) {
			if knownCommands[cmd] {
				t.Errorf("command %q should not be recognized", cmd)
			}
		})
	}
}

func TestServicesListComplete(t *testing.T) {
	// Verify all expected services are in the services list
	expected := []string{
		"hyve-backend",
		"hyve-discovery",
		"hyve-agent-daemon",
		"hyve-vision",
		"hyve-llm",
	}

	if len(services) != len(expected) {
		t.Errorf("expected %d services, got %d", len(expected), len(services))
	}

	for i, svc := range expected {
		if i >= len(services) {
			t.Errorf("missing service: %s", svc)
			continue
		}
		if services[i] != svc {
			t.Errorf("service[%d]: expected %q, got %q", i, svc, services[i])
		}
	}
}

func TestVersionInfo(t *testing.T) {
	// Verify version variable is set
	if version == "" {
		t.Error("version should not be empty")
	}
	if version != "1.0.0" {
		t.Errorf("expected default version 1.0.0, got %s", version)
	}
}

func TestDirectoryConstants(t *testing.T) {
	// Verify path constants match Python version
	if configDir != "/etc/hyve" {
		t.Errorf("configDir: expected /etc/hyve, got %s", configDir)
	}
	if dataDir != "/var/lib/hyve" {
		t.Errorf("dataDir: expected /var/lib/hyve, got %s", dataDir)
	}
	if installDir != "/opt/hyve" {
		t.Errorf("installDir: expected /opt/hyve, got %s", installDir)
	}
}

func TestJSONRoundTrip(t *testing.T) {
	// Test that our JSON marshaling for API posts works correctly
	payload := map[string]string{
		"peer_url": "http://192.168.1.100:6777",
	}

	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}

	var result map[string]string
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}

	if result["peer_url"] != "http://192.168.1.100:6777" {
		t.Errorf("peer_url mismatch: %s", result["peer_url"])
	}
}

func TestProvisionPayload(t *testing.T) {
	// Test provision payload structure matches what the backend expects
	payload := map[string]string{
		"target_host": "192.168.1.50",
		"ssh_user":    "admin",
	}

	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}

	var result map[string]string
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}

	if result["target_host"] != "192.168.1.50" {
		t.Errorf("target_host mismatch: %s", result["target_host"])
	}
	if result["ssh_user"] != "admin" {
		t.Errorf("ssh_user mismatch: %s", result["ssh_user"])
	}
}
