// hart-cli — HART OS command-line interface.
//
// A zero-dependency Go rewrite of hart-cli.py with <3ms startup.
// Build: go build -ldflags "-X main.version=1.0.0 -X main.buildTime=$(date -u +%Y-%m-%dT%H:%M:%SZ) -X main.gitCommit=$(git rev-parse --short HEAD)" -o hart .
// Install: sudo cp hart /usr/local/bin/hart
package main

import (
	"fmt"
	"os"
	"strconv"
)

// version, buildTime, and gitCommit are set via ldflags at build time.
var (
	version   = "1.0.0"
	buildTime = ""
	gitCommit = ""
)

const usage = `HART OS CLI - Manage your agentic intelligence node

Usage:
    hart <command> [arguments]

Commands:
    status              Show all service states + node identity
    start               Start all HART OS services
    stop                Stop all HART OS services
    restart             Restart all HART OS services
    logs [SERVICE]      View service logs (default: all)
         -n N           Number of lines (default: 50)
         -f             Follow log output
    health              Node health report (tier, peers, trust)
    join PEER_URL       Join an existing hive network
    provision HOST      Provision HART OS on a remote machine
         -u USER        SSH user (default: root)
    update              Update HART OS to latest version
    node-id             Print this node's Ed25519 public key
    version             Show HART OS version and build info
`

func main() {
	if len(os.Args) < 2 {
		fmt.Print(usage)
		os.Exit(0)
	}

	command := os.Args[1]

	switch command {
	case "status":
		cmdStatus()

	case "start":
		cmdStart()

	case "stop":
		cmdStop()

	case "restart":
		cmdRestart()

	case "logs":
		service := ""
		lines := 50
		follow := false

		// Parse logs subcommand args
		i := 2
		for i < len(os.Args) {
			arg := os.Args[i]
			switch arg {
			case "-f", "--follow":
				follow = true
			case "-n", "--lines":
				i++
				if i < len(os.Args) {
					if n, err := strconv.Atoi(os.Args[i]); err == nil {
						lines = n
					}
				}
			default:
				// Positional: service name
				if service == "" && len(arg) > 0 && arg[0] != '-' {
					service = arg
				}
			}
			i++
		}
		cmdLogs(service, lines, follow)

	case "health":
		cmdHealth()

	case "join":
		if len(os.Args) < 3 {
			fmt.Println("Usage: hart join PEER_URL")
			os.Exit(1)
		}
		cmdJoin(os.Args[2])

	case "provision":
		if len(os.Args) < 3 {
			fmt.Println("Usage: hart provision HOST [-u USER]")
			os.Exit(1)
		}
		host := os.Args[2]
		user := "root"
		// Parse -u/--user flag
		for i := 3; i < len(os.Args); i++ {
			if (os.Args[i] == "-u" || os.Args[i] == "--user") && i+1 < len(os.Args) {
				user = os.Args[i+1]
				break
			}
		}
		cmdProvision(host, user)

	case "update":
		cmdUpdate()

	case "node-id":
		cmdNodeID()

	case "version":
		cmdVersion()

	case "-h", "--help", "help":
		fmt.Print(usage)

	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n\n", command)
		fmt.Print(usage)
		os.Exit(1)
	}
}
