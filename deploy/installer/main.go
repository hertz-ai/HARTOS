// HART OS Universal Installer
// Cross-compiles to Windows (.exe), macOS (.app), Linux (ELF)
// Double-click: detects OS → downloads correct installer → runs it
//
// Build all platforms:
//   GOOS=windows GOARCH=amd64 go build -o hevolve-install.exe
//   GOOS=darwin  GOARCH=arm64 go build -o hevolve-install-macos
//   GOOS=linux   GOARCH=amd64 go build -o hevolve-install-linux

package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	repoOwner  = "hertz-ai"
	repoNunba  = "Nunba-HART-Companion"
	repoHARTOS = "HARTOS"
	apiBase    = "https://api.github.com/repos"
)

var assetMap = map[string][]string{
	"windows/amd64": {"Nunba_Setup.exe", "Nunba-Setup.exe"},
	"windows/arm64": {"Nunba_Setup.exe", "Nunba-Setup.exe"},
	"darwin/amd64":  {"Nunba.dmg", "Nunba-Setup.dmg"},
	"darwin/arm64":  {"Nunba.dmg", "Nunba-Setup.dmg"},
	"linux/amd64":   {"Nunba.AppImage", "Nunba-x86_64.AppImage"},
	"linux/arm64":   {"Nunba-arm64.AppImage"},
}

var isoMap = map[string][]string{
	"server":  {"hart-os-*-server-x86_64-linux.iso"},
	"desktop": {"hart-os-*-desktop-x86_64-linux.iso"},
	"edge":    {"hart-os-*-edge-x86_64-linux.iso"},
}

type Release struct {
	TagName string  `json:"tag_name"`
	Assets  []Asset `json:"assets"`
}

type Asset struct {
	Name               string `json:"name"`
	BrowserDownloadURL string `json:"browser_download_url"`
	Size               int64  `json:"size"`
}

// Non-interactive flags — used by .github/workflows/remote-deploy.yml and any
// scripted/CI install. When --mode is set the menu is skipped entirely; when
// --yes is set every confirmation defaults to its safe value.
var (
	flagMode = flag.String("mode", "",
		"Non-interactive deployment mode: nunba, docker, server, desktop, edge, pip")
	flagTier = flag.String("tier", "flat",
		"HARTOS tier when --mode=docker: central, regional, flat (default flat)")
	flagYes = flag.Bool("yes", false,
		"Assume yes / non-interactive — skip all prompts and use safe defaults")
	flagPort = flag.Int("port", 6777,
		"HARTOS backend port for --mode=docker (default 6777)")
	flagImage = flag.String("image", "ghcr.io/hertz-ai/hartos:latest",
		"Container image for --mode=docker (default ghcr.io/hertz-ai/hartos:latest)")
)

func main() {
	flag.Parse()

	fmt.Println()
	fmt.Println("  HART OS - Hevolve Installer")
	fmt.Println("  Intelligence for everyone.")
	fmt.Println()

	platform := runtime.GOOS + "/" + runtime.GOARCH
	fmt.Printf("  Platform: %s/%s\n\n", runtime.GOOS, runtime.GOARCH)

	// Non-interactive path: skip menu when --mode is given.
	if *flagMode != "" {
		dispatchMode(*flagMode, platform)
		return
	}

	fmt.Println("  1. Nunba (companion app for your existing OS)")
	fmt.Println("  2. HART OS Server ISO (headless, servers, RPi)")
	fmt.Println("  3. HART OS Desktop ISO (full GNOME desktop)")
	fmt.Println("  4. HART OS Edge ISO (minimal, IoT)")
	fmt.Println("  5. pip install (developer)")
	fmt.Println("  6. HARTOS Docker (server / cloud — pulls signed image)")
	fmt.Print("\n  Choice [1]: ")

	var choice string
	fmt.Scanln(&choice)
	if choice == "" {
		choice = "1"
	}

	switch choice {
	case "1":
		installNunba(platform)
	case "2":
		downloadISO("server")
	case "3":
		downloadISO("desktop")
	case "4":
		downloadISO("edge")
	case "5":
		pipInstall()
	case "6":
		installDocker(*flagTier, *flagPort, *flagImage)
	default:
		fmt.Println("Invalid choice.")
		os.Exit(1)
	}
}

// dispatchMode is the non-interactive entry point.  Used by the
// remote-deploy.yml workflow and any `--mode X` CLI invocation.
// Keeps the same handlers as the menu so there is exactly one
// implementation per mode.
func dispatchMode(mode, platform string) {
	switch strings.ToLower(mode) {
	case "nunba":
		installNunba(platform)
	case "server":
		downloadISO("server")
	case "desktop":
		downloadISO("desktop")
	case "edge":
		downloadISO("edge")
	case "pip":
		pipInstall()
	case "docker":
		installDocker(*flagTier, *flagPort, *flagImage)
	default:
		fmt.Printf("Unknown mode: %s\n", mode)
		fmt.Println("Valid modes: nunba, docker, server, desktop, edge, pip")
		os.Exit(1)
	}
}

func installNunba(platform string) {
	patterns, ok := assetMap[platform]
	if !ok {
		fmt.Printf("No Nunba installer for %s. Try: pip install hart-backend\n", platform)
		os.Exit(1)
	}

	fmt.Println("Fetching latest release...")
	release, err := getLatestRelease(repoOwner, repoNunba)
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		os.Exit(1)
	}

	asset := findAsset(release, patterns)
	if asset == nil {
		fmt.Printf("No installer found in %s. Assets:\n", release.TagName)
		for _, a := range release.Assets {
			fmt.Printf("  - %s\n", a.Name)
		}
		os.Exit(1)
	}

	fmt.Printf("Downloading %s (%d MB)...\n", asset.Name, asset.Size/1024/1024)
	dest := filepath.Join(os.TempDir(), asset.Name)
	if err := downloadFile(dest, asset.BrowserDownloadURL); err != nil {
		fmt.Printf("Download failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Println("Downloaded.")

	// Verify SHA-256 if .sha256 file exists in release
	if sha := findAsset(release, []string{asset.Name + ".sha256"}); sha != nil {
		fmt.Print("Verifying checksum... ")
		if verifyChecksum(dest, sha.BrowserDownloadURL) {
			fmt.Println("valid.")
		} else {
			fmt.Println("MISMATCH. File may be corrupt.")
			os.Exit(1)
		}
	}

	fmt.Println("Launching installer...")
	switch runtime.GOOS {
	case "windows":
		exec.Command("cmd", "/C", dest).Start()
	case "darwin":
		exec.Command("open", dest).Start()
	case "linux":
		os.Chmod(dest, 0755)
		exec.Command(dest).Start()
	}
	fmt.Println("Done. Follow the on-screen instructions.")
}

func downloadISO(variant string) {
	fmt.Printf("Fetching latest HART OS %s ISO...\n", variant)
	release, err := getLatestRelease(repoOwner, repoHARTOS)
	if err != nil {
		fmt.Printf("No release found: %v\n", err)
		fmt.Println("ISOs available at: https://github.com/hertz-ai/HARTOS/actions")
		os.Exit(1)
	}

	asset := findAsset(release, isoMap[variant])
	if asset == nil {
		fmt.Printf("No %s ISO in release %s\n", variant, release.TagName)
		os.Exit(1)
	}

	home, _ := os.UserHomeDir()
	dest := filepath.Join(home, "Downloads", asset.Name)
	fmt.Printf("Downloading %s (%d MB)...\n", asset.Name, asset.Size/1024/1024)
	if err := downloadFile(dest, asset.BrowserDownloadURL); err != nil {
		fmt.Printf("Download failed: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("\nISO saved to %s\n", dest)
	fmt.Println("Flash to USB:")
	fmt.Printf("  sudo dd if=%s of=/dev/sdX bs=4M status=progress\n", dest)
	fmt.Println("  Or use Balena Etcher / Rufus.")
}

// installDocker is the workflow-driven server-deploy path.  It pulls the
// signed HARTOS container image and runs it with the right tier/port.  It
// installs Docker first if missing (Linux only — Windows/macOS users need
// Docker Desktop, which the installer points them to instead of bringing
// it in headless).
//
// The container args mirror the canonical scripts/start_docker.sh tier
// logic (HEVOLVE_NODE_TIER env var, --restart unless-stopped, port map,
// log + image volume mounts).  Keep these in sync.
func installDocker(tier string, port int, image string) {
	if tier != "central" && tier != "regional" && tier != "flat" {
		fmt.Printf("Invalid --tier %q.  Use central, regional, or flat.\n", tier)
		os.Exit(1)
	}

	fmt.Printf("  HARTOS Docker install (tier=%s, port=%d)\n", tier, port)
	fmt.Printf("  Image: %s\n\n", image)

	// 1. Ensure Docker is installed.
	if _, err := exec.LookPath("docker"); err != nil {
		fmt.Println("  Docker not found.")
		switch runtime.GOOS {
		case "linux":
			fmt.Println("  Installing Docker via get.docker.com convenience script...")
			cmd := exec.Command("sh", "-c", "curl -fsSL https://get.docker.com | sh")
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			if err := cmd.Run(); err != nil {
				fmt.Printf("  Docker install failed: %v\n", err)
				os.Exit(1)
			}
		case "darwin":
			fmt.Println("  Install Docker Desktop manually: https://www.docker.com/products/docker-desktop/")
			os.Exit(1)
		case "windows":
			fmt.Println("  Install Docker Desktop manually: https://www.docker.com/products/docker-desktop/")
			fmt.Println("  Or, on Windows Server: https://learn.microsoft.com/virtualization/windowscontainers/quick-start/install-1903")
			os.Exit(1)
		}
	}

	// 2. Pull image.
	fmt.Printf("  Pulling %s ...\n", image)
	pullCmd := exec.Command("docker", "pull", image)
	pullCmd.Stdout = os.Stdout
	pullCmd.Stderr = os.Stderr
	if err := pullCmd.Run(); err != nil {
		fmt.Printf("  docker pull failed: %v\n", err)
		os.Exit(1)
	}

	// 3. Stop any existing hartos container so we don't collide on the
	// container name + bound port.  -f silences the "no such container"
	// stderr noise on a fresh box.
	exec.Command("docker", "rm", "-f", "hartos").Run()

	// 4. Run.  Args mirror scripts/start_docker.sh:
	//   - port maps backend
	//   - tier env drives boot-time deployment-mode selection
	//   - restart=unless-stopped survives reboots without auto-launching
	//     after an explicit `docker stop`
	//   - logs and output_images persist across container recreations
	logsHost := filepath.Join(homeOrTmp(), ".hart", "logs")
	imagesHost := filepath.Join(homeOrTmp(), ".hart", "output_images")
	os.MkdirAll(logsHost, 0o755)
	os.MkdirAll(imagesHost, 0o755)

	args := []string{
		"run", "-d",
		"--name", "hartos",
		"--restart", "unless-stopped",
		"-p", fmt.Sprintf("%d:%d", port, port),
		"-e", "HEVOLVE_NODE_TIER=" + tier,
		"-v", logsHost + ":/app/logs",
		"-v", imagesHost + ":/app/output_images",
		image,
	}
	fmt.Printf("  docker %s\n", strings.Join(args, " "))
	runCmd := exec.Command("docker", args...)
	runCmd.Stdout = os.Stdout
	runCmd.Stderr = os.Stderr
	if err := runCmd.Run(); err != nil {
		fmt.Printf("  docker run failed: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("\n  HARTOS container started.\n")
	fmt.Printf("  Backend:   http://localhost:%d\n", port)
	fmt.Printf("  Tail logs: docker logs -f hartos\n")
	fmt.Printf("  Stop:      docker stop hartos\n")
}

// homeOrTmp returns $HOME if available, falling back to OS temp.  Used by
// installDocker to pick a stable host-side path for log/image volumes
// without requiring root.
func homeOrTmp() string {
	if h, err := os.UserHomeDir(); err == nil && h != "" {
		return h
	}
	return os.TempDir()
}

func pipInstall() {
	fmt.Println("Installing via pip...")
	cmd := exec.Command("pip", "install", "git+https://github.com/hertz-ai/HARTOS.git")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Println("\nManual install:")
		fmt.Println("  git clone https://github.com/hertz-ai/HARTOS.git")
		fmt.Println("  cd HARTOS && pip install -r requirements.txt")
		fmt.Println("  python hart_intelligence_entry.py")
	}
}

func getLatestRelease(owner, repo string) (*Release, error) {
	url := fmt.Sprintf("%s/%s/%s/releases/latest", apiBase, owner, repo)
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var r Release
	json.NewDecoder(resp.Body).Decode(&r)
	return &r, nil
}

func findAsset(release *Release, patterns []string) *Asset {
	for _, p := range patterns {
		for i := range release.Assets {
			if strings.Contains(p, "*") {
				if matched, _ := filepath.Match(p, release.Assets[i].Name); matched {
					return &release.Assets[i]
				}
			} else if release.Assets[i].Name == p {
				return &release.Assets[i]
			}
		}
	}
	return nil
}

func downloadFile(dest, url string) error {
	out, err := os.Create(dest)
	if err != nil {
		return err
	}
	defer out.Close()
	resp, err := http.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	c := &writeCounter{Total: resp.ContentLength}
	_, err = io.Copy(out, io.TeeReader(resp.Body, c))
	fmt.Println()
	return err
}

type writeCounter struct {
	Written, Total int64
}

func (w *writeCounter) Write(p []byte) (int, error) {
	n := len(p)
	w.Written += int64(n)
	if w.Total > 0 {
		fmt.Printf("\r  %.0f%% (%d/%d MB)", float64(w.Written)/float64(w.Total)*100,
			w.Written/1024/1024, w.Total/1024/1024)
	}
	return n, nil
}

func verifyChecksum(file, checksumURL string) bool {
	resp, err := http.Get(checksumURL)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	expected := strings.Fields(string(body))[0]

	f, err := os.Open(file)
	if err != nil {
		return false
	}
	defer f.Close()
	h := sha256.New()
	io.Copy(h, f)
	return hex.EncodeToString(h.Sum(nil)) == expected
}
