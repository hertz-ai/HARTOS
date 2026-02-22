package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	defaultBackendPort = 6777
	envFilePath        = "/etc/hart/hart.env"
	httpGetTimeout     = 5 * time.Second
	httpPostTimeout    = 30 * time.Second
)

// getBackendPort reads HART_BACKEND_PORT from /etc/hart/hart.env.
// Returns defaultBackendPort (6777) if the file doesn't exist or the key isn't found.
func getBackendPort() int {
	f, err := os.Open(envFilePath)
	if err != nil {
		return defaultBackendPort
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "HART_BACKEND_PORT=") {
			val := strings.TrimSpace(strings.SplitN(line, "=", 2)[1])
			if val == "" {
				return defaultBackendPort
			}
			port, err := strconv.Atoi(val)
			if err != nil {
				return defaultBackendPort
			}
			return port
		}
	}
	return defaultBackendPort
}

// getBackendPortFromReader parses HART_BACKEND_PORT from an arbitrary reader.
// Exported for testing.
func getBackendPortFromReader(r io.Reader) int {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "HART_BACKEND_PORT=") {
			val := strings.TrimSpace(strings.SplitN(line, "=", 2)[1])
			if val == "" {
				return defaultBackendPort
			}
			port, err := strconv.Atoi(val)
			if err != nil {
				return defaultBackendPort
			}
			return port
		}
	}
	return defaultBackendPort
}

// backendURL builds a full URL to the local backend.
func backendURL(path string) string {
	return fmt.Sprintf("http://localhost:%d%s", getBackendPort(), path)
}

// apiGet performs a GET request to the local backend and returns the raw body.
func apiGet(path string) ([]byte, error) {
	client := &http.Client{Timeout: httpGetTimeout}
	resp, err := client.Get(backendURL(path))
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body failed: %w", err)
	}

	if resp.StatusCode >= 400 {
		return body, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	return body, nil
}

// apiPost performs a POST request with JSON body to the local backend.
func apiPost(path string, data interface{}) ([]byte, error) {
	payload, err := json.Marshal(data)
	if err != nil {
		return nil, fmt.Errorf("marshal failed: %w", err)
	}

	client := &http.Client{Timeout: httpPostTimeout}
	resp, err := client.Post(
		backendURL(path),
		"application/json",
		bytes.NewReader(payload),
	)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body failed: %w", err)
	}

	if resp.StatusCode >= 400 {
		return body, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	return body, nil
}
