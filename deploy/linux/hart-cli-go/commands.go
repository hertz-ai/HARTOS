package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

const (
	configDir  = "/etc/hart"
	dataDir    = "/var/lib/hart"
	installDir = "/opt/hart"
)

var services = []string{
	"hart-backend",
	"hart-discovery",
	"hart-agent-daemon",
	"hart-vision",
	"hart-llm",
}

// ANSI color helpers
const (
	colorCyan   = "\033[36m"
	colorGreen  = "\033[32m"
	colorYellow = "\033[33m"
	colorGray   = "\033[90m"
	colorRed    = "\033[31m"
	colorReset  = "\033[0m"
)

// cmdStatus shows status of all HART OS services and node identity.
func cmdStatus() {
	fmt.Printf("%sHART OS %s%s\n\n", colorCyan, version, colorReset)

	// Node ID
	pubKeyPath := dataDir + "/node_public.key"
	if data, err := os.ReadFile(pubKeyPath); err == nil {
		nodeID := hex.EncodeToString(data)
		if len(nodeID) > 16 {
			nodeID = nodeID[:16]
		}
		fmt.Printf("  Node ID:  %s...\n", nodeID)
	} else {
		fmt.Println("  Node ID:  not generated")
	}

	fmt.Printf("  Backend:  http://localhost:%d\n\n", getBackendPort())

	// Service statuses
	maxName := 0
	for _, svc := range services {
		if len(svc) > maxName {
			maxName = len(svc)
		}
	}

	for _, svc := range services {
		status := getServiceStatus(svc)
		var color, symbol string
		switch status {
		case "active":
			color = colorGreen
			symbol = "\u25cf" // ●
		case "activating":
			color = colorYellow
			symbol = "\u25d0" // ◐
		default:
			color = colorGray
			symbol = "\u25cb" // ○
			status = "inactive"
		}
		fmt.Printf("  %s%s%s %-*s  %s%s%s\n", color, symbol, colorReset, maxName, svc, color, status, colorReset)
	}

	// Backend health check
	_, err := apiGet("/status")
	if err == nil {
		fmt.Println("\n  Backend responding: yes")
	} else {
		fmt.Printf("\n  Backend responding: %sno%s\n", colorRed, colorReset)
	}
}

// getServiceStatus runs systemctl is-active for a given service.
func getServiceStatus(svc string) string {
	out, err := exec.Command("systemctl", "is-active", svc+".service").Output()
	if err != nil {
		return "inactive"
	}
	return strings.TrimSpace(string(out))
}

// cmdStart starts all HART OS services via systemd.
func cmdStart() {
	fmt.Println("Starting HART OS services...")
	cmd := exec.Command("sudo", "systemctl", "start", "hart.target")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	_ = cmd.Run()
	fmt.Println("Done. Run 'hart status' to check.")
}

// cmdStop stops all HART OS services via systemd.
func cmdStop() {
	fmt.Println("Stopping HART OS services...")
	cmd := exec.Command("sudo", "systemctl", "stop", "hart.target")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	_ = cmd.Run()
	fmt.Println("Done.")
}

// cmdRestart restarts all HART OS services via systemd.
func cmdRestart() {
	fmt.Println("Restarting HART OS services...")
	cmd := exec.Command("sudo", "systemctl", "restart", "hart.target")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	_ = cmd.Run()
	fmt.Println("Done. Run 'hart status' to check.")
}

// cmdLogs shows HART OS service logs via journalctl.
func cmdLogs(service string, lines int, follow bool) {
	if service == "" {
		service = "hart-*"
	}

	args := []string{"-u", service, "--no-pager", "-n", fmt.Sprintf("%d", lines)}
	if follow {
		args = append(args, "-f")
	}

	cmd := exec.Command("journalctl", args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	_ = cmd.Run()
}

// cmdHealth shows the node health report.
func cmdHealth() {
	body, err := apiGet("/api/social/dashboard/health")
	if err == nil {
		fmt.Printf("%sNode Health Report%s\n\n", colorCyan, colorReset)

		var data map[string]interface{}
		if jsonErr := json.Unmarshal(body, &data); jsonErr == nil {
			for key, val := range data {
				fmt.Printf("  %s: %v\n", key, val)
			}
			return
		}
		// If JSON parse fails, print raw
		fmt.Printf("  %s\n", string(body))
		return
	}

	// Fallback to basic /status
	_, statusErr := apiGet("/status")
	if statusErr == nil {
		fmt.Printf("%sNode Health Report%s\n\n", colorCyan, colorReset)
		fmt.Println("  Status: running")
		fmt.Printf("  Port: %d\n", getBackendPort())
	} else {
		fmt.Println("Backend not responding. Run 'hart start' first.")
	}
}

// cmdJoin joins an existing hive network by announcing to a peer.
func cmdJoin(peerURL string) {
	fmt.Printf("Joining hive at %s...\n", peerURL)

	payload := map[string]string{"peer_url": peerURL}
	body, err := apiPost("/api/social/peers/announce", payload)
	if err != nil {
		fmt.Printf("Failed: %v\n", err)
		return
	}

	var result map[string]interface{}
	if jsonErr := json.Unmarshal(body, &result); jsonErr == nil {
		if _, hasError := result["error"]; hasError {
			fmt.Printf("Failed: %v\n", result)
			return
		}
	}
	fmt.Println("Join request sent successfully.")
}

// cmdProvision provisions HART OS on a remote machine via SSH.
func cmdProvision(host, user string) {
	if user == "" {
		user = "root"
	}
	fmt.Printf("Provisioning HART OS on %s@%s...\n", user, host)

	payload := map[string]string{
		"target_host": host,
		"ssh_user":    user,
	}
	body, err := apiPost("/api/provision/deploy", payload)
	if err != nil {
		fmt.Printf("Failed: %v\n", err)
		return
	}

	var result map[string]interface{}
	if jsonErr := json.Unmarshal(body, &result); jsonErr == nil {
		if _, hasError := result["error"]; hasError {
			fmt.Printf("Failed: %v\n", result)
			return
		}
		fmt.Println("Provisioning started. Track with: hart status")
		if nodeID, ok := result["node_id"]; ok {
			fmt.Printf("Remote node ID: %v\n", nodeID)
		}
		return
	}
	fmt.Println("Provisioning started. Track with: hart status")
}

// cmdUpdate triggers an OTA update or git pull depending on installation type.
func cmdUpdate() {
	fmt.Println("Checking for updates...")

	gitDir := installDir + "/.git"
	if _, err := os.Stat(gitDir); err == nil {
		// Git-based installation: pull and restart
		cmd := exec.Command("git", "-C", installDir, "pull")
		out, err := cmd.CombinedOutput()
		output := strings.TrimSpace(string(out))
		if err != nil {
			fmt.Printf("Update failed: %s\n", output)
			return
		}
		fmt.Printf("Updated: %s\n", output)
		fmt.Println("Restarting services...")
		restartCmd := exec.Command("sudo", "systemctl", "restart", "hart.target")
		restartCmd.Stdout = os.Stdout
		restartCmd.Stderr = os.Stderr
		_ = restartCmd.Run()
		fmt.Println("Done.")
	} else {
		// No git repo: trigger OTA update service
		fmt.Println("No git repository found. Triggering OTA update service...")
		cmd := exec.Command("sudo", "systemctl", "restart", "hart-update.service")
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			fmt.Println("OTA update service not available. Manual update required.")
			fmt.Printf("  1. Download latest bundle\n")
			fmt.Printf("  2. Extract to %s\n", installDir)
			fmt.Printf("  3. Run: sudo systemctl restart hart.target\n")
		} else {
			fmt.Println("OTA update triggered. Run 'hart status' to check.")
		}
	}
}

// cmdNodeID prints this node's Ed25519 public key in hex.
func cmdNodeID() {
	pubKeyPath := dataDir + "/node_public.key"
	data, err := os.ReadFile(pubKeyPath)
	if err != nil {
		fmt.Println("Node identity not generated. Run install.sh first.")
		os.Exit(1)
	}
	fmt.Println(hex.EncodeToString(data))
}

// cmdVersion shows version and build information.
func cmdVersion() {
	fmt.Printf("HART OS %s\n", version)
	if buildTime != "" {
		fmt.Printf("Built:   %s\n", buildTime)
	}
	if gitCommit != "" {
		fmt.Printf("Commit:  %s\n", gitCommit)
	}
	fmt.Printf("Install: %s\n", installDir)
	fmt.Printf("Config:  %s\n", configDir)
	fmt.Printf("Data:    %s\n", dataDir)

	// Show OS pretty name if available
	if data, err := os.ReadFile("/etc/os-release"); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			if strings.HasPrefix(line, "PRETTY_NAME=") {
				name := strings.TrimSpace(strings.SplitN(line, "=", 2)[1])
				name = strings.Trim(name, "\"")
				fmt.Printf("OS:      %s\n", name)
				break
			}
		}
	}
}

// parseStatusResponse parses a JSON status response into a map.
// Exported for testing.
func parseStatusResponse(data []byte) (map[string]interface{}, error) {
	var result map[string]interface{}
	err := json.Unmarshal(data, &result)
	return result, err
}

// parseHealthResponse parses a JSON health response into a map.
// Exported for testing.
func parseHealthResponse(data []byte) (map[string]interface{}, error) {
	var result map[string]interface{}
	err := json.Unmarshal(data, &result)
	return result, err
}
