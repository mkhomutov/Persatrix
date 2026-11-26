package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Real-process tests of how the orchestrator starts (ISSUE-0204).
//
// What these tests are about is decided in main(): whether the agent-facing
// port opens, and whether the process starts at all. No test of a helper
// shows that main() calls the helper, so these run main() itself. The test
// binary starts itself again with runOrchestratorEnv set, and TestMain then
// runs main() in place of the tests: a real process, with its own flags,
// ports, log output and exit status.

// runOrchestratorEnv, set to "1", makes this test binary run the
// orchestrator instead of its tests.
const runOrchestratorEnv = "PERSATRIX_TEST_RUN_ORCHESTRATOR"

// processLimit bounds every wait on the orchestrator process: for its health
// check to answer, and for it to exit. A process that does neither in this
// time fails the test and is killed.
const processLimit = 30 * time.Second

func TestMain(m *testing.M) {
	if os.Getenv(runOrchestratorEnv) == "1" {
		main() // returns after a clean stop; a refused start exits inside it
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// lockedBuffer collects a process's output while the test reads it.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// orchestratorProcess is one orchestrator started by a test.
type orchestratorProcess struct {
	t        *testing.T
	cmd      *exec.Cmd
	httpAddr string // the REST API
	grpcAddr string // the port agents call
	dataDir  string // where it runs and keeps its audit log and its stores
	output   *lockedBuffer
	exited   chan struct{} // closed once the process has exited
}

// repoRoot is the checkout, two folders above this package.
func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", ".."))
	require.NoError(t, err)
	return root
}

func freeLoopbackPort(t *testing.T) int {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	defer lis.Close() //nolint:errcheck
	return lis.Addr().(*net.TCPAddr).Port
}

// startOrchestrator starts the orchestrator on free loopback ports, reading
// configDir, with its log buffer under logBufferDir. Everything it writes
// goes under a temporary folder, and nothing of the test's own environment
// reaches it but what a process needs to run. It is killed at the end of the
// test if it is still running.
func startOrchestrator(t *testing.T, configDir, logBufferDir string) *orchestratorProcess {
	t.Helper()
	return startOrchestratorOn(t, configDir, logBufferDir, freeLoopbackPort(t))
}

// startOrchestratorOn is startOrchestrator with the agent-facing port chosen
// by the caller, for the test that keeps that port taken, and with any more
// flags the caller wants on the command line.
func startOrchestratorOn(t *testing.T, configDir, logBufferDir string, grpcPort int, moreFlags ...string) *orchestratorProcess {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("stops the process with SIGTERM, which Windows does not have")
	}
	exe, err := os.Executable()
	require.NoError(t, err)
	data := t.TempDir()
	httpPort := freeLoopbackPort(t)
	p := &orchestratorProcess{
		t:        t,
		httpAddr: fmt.Sprintf("127.0.0.1:%d", httpPort),
		grpcAddr: fmt.Sprintf("127.0.0.1:%d", grpcPort),
		dataDir:  data,
		output:   &lockedBuffer{},
		exited:   make(chan struct{}),
	}
	p.cmd = exec.Command(exe, append([]string{ //nolint:gosec // the test binary itself
		"--config", configDir,
		"--workflows-dir", filepath.Join(repoRoot(t), "workflows"),
		"--http-port", fmt.Sprint(httpPort),
		"--port", fmt.Sprint(grpcPort),
		"--channels-db", filepath.Join(data, "channels.db"),
		"--accounts-db", filepath.Join(data, "accounts.db"),
	}, moreFlags...)...)
	p.cmd.Dir = data
	p.cmd.Env = []string{
		runOrchestratorEnv + "=1",
		"PATH=" + os.Getenv("PATH"),
		"HOME=" + os.Getenv("HOME"),
		"TMPDIR=" + os.Getenv("TMPDIR"),
		"PERSATRIX_LOGBUFFER_DIR=" + logBufferDir,
		"OBSERVABILITY_AUDIT_PATH=" + filepath.Join(data, "audit.jsonl"),
		// Nothing listens there: traces and metrics go nowhere.
		"OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:1",
	}
	p.cmd.Stdout, p.cmd.Stderr = p.output, p.output
	require.NoError(t, p.cmd.Start())
	go func() {
		_ = p.cmd.Wait()
		close(p.exited)
	}()
	t.Cleanup(func() {
		select {
		case <-p.exited:
		default:
			_ = p.cmd.Process.Kill()
			<-p.exited
		}
	})
	return p
}

// healthz waits for the health check to answer 200 and returns its body.
func (p *orchestratorProcess) healthz() string {
	p.t.Helper()
	client := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(processLimit)
	for time.Now().Before(deadline) {
		select {
		case <-p.exited:
			p.t.Fatalf("the orchestrator exited before its health check answered:\n%s", p.output)
		default:
		}
		resp, err := client.Get("http://" + p.httpAddr + "/healthz")
		if err == nil {
			body, readErr := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if readErr == nil && resp.StatusCode == http.StatusOK {
				return string(body)
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	p.t.Fatalf("the health check had not answered 200 after %v:\n%s", processLimit, p.output)
	return ""
}

// waitExit waits for the process to exit and returns its exit status.
func (p *orchestratorProcess) waitExit() int {
	p.t.Helper()
	select {
	case <-p.exited:
	case <-time.After(processLimit):
		p.t.Fatalf("the orchestrator was still running after %v:\n%s", processLimit, p.output)
	}
	return p.cmd.ProcessState.ExitCode()
}

// stop asks the process to stop, as an operator or a container runtime does,
// and returns its exit status.
func (p *orchestratorProcess) stop() int {
	p.t.Helper()
	require.NoError(p.t, p.cmd.Process.Signal(syscall.SIGTERM))
	return p.waitExit()
}

// shippedConfig is the config folder as the repository ships it.
func shippedConfig(t *testing.T) string {
	t.Helper()
	return filepath.Join(repoRoot(t), "config")
}

// copyOfShippedConfig copies the shipped config folder, so a test can break
// one file of it and start the orchestrator on everything else as shipped.
func copyOfShippedConfig(t *testing.T) string {
	t.Helper()
	src, dst := shippedConfig(t), t.TempDir()
	require.NoError(t, filepath.WalkDir(src, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if d.IsDir() {
			return os.MkdirAll(filepath.Join(dst, rel), 0o755)
		}
		data, err := os.ReadFile(path) //nolint:gosec // the repository's own config
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(dst, rel), data, 0o644) //nolint:gosec
	}))
	return dst
}

// aFolderThatCannotBeMade is a path under a regular file, so the log buffer
// cannot create its folder there, whoever the process runs as.
func aFolderThatCannotBeMade(t *testing.T) string {
	t.Helper()
	file := filepath.Join(t.TempDir(), "a-file")
	require.NoError(t, os.WriteFile(file, []byte("x"), 0o600))
	return filepath.Join(file, "logs")
}

// TestOrchestratorProcess_ServesAgents: the agent-facing port is open and the
// wallet answers on it whether or not the log buffer started, and the health
// check, still 200 with status "ok", names the buffer when it did not. The
// wallet's reaper runs in both cases: without it a lease an agent never
// settles is held for good, and the agent's budget fills up with them.
//
// Before ISSUE-0204's fix the second case answered `{"status":"ok"}` with
// nothing listening on the gRPC port, so every agent's model call was
// refused for want of a wallet lease.
func TestOrchestratorProcess_ServesAgents(t *testing.T) {
	tests := []struct {
		name         string
		logBufferDir func(*testing.T) string
		wantHealthz  string
		wantLogCode  codes.Code // how a shipped batch of agent logs ends
		wantInOutput []string
	}{
		{
			name:         "the shipped config, the log buffer started",
			logBufferDir: func(t *testing.T) string { return filepath.Join(t.TempDir(), "logs") },
			wantHealthz:  `{"status":"ok"}`,
			wantLogCode:  codes.OK,
		},
		{
			name:         "the shipped config, the log buffer cannot make its folder",
			logBufferDir: aFolderThatCannotBeMade,
			wantHealthz:  `{"status":"ok","missing":["log_buffer"]}`,
			wantLogCode:  codes.Unimplemented,
			wantInOutput: []string{
				"the log buffer did not start, so the orchestrator runs without it: " +
					"the log routes answer 501 and logs that agents ship are refused; " +
					"agents are served all the same, the wallet included",
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := startOrchestrator(t, shippedConfig(t), tt.logBufferDir(t))

			assert.JSONEq(t, tt.wantHealthz, p.healthz())

			cc := dialAgentPort(t, p.grpcAddr)
			lease, err := acquireProbeLease(cc)
			require.NoError(t, err, "the wallet answers on the agent-facing port\n%s", p.output)
			assert.NotEmpty(t, lease.GetGrant().GetLeaseId(), "and grants a lease inside the shipped limits")
			assert.Equal(t, tt.wantLogCode, status.Code(shipProbeBatch(cc)))

			assert.Equal(t, 0, p.stop(), "a stop on request is a clean exit\n%s", p.output)
			out := p.output.String()
			for _, want := range append(tt.wantInOutput,
				"wallet lease enforcement initialized", "gRPC server listening", "wallet: reaper started") {
				assert.Contains(t, out, want)
			}
			// Where a served start keeps its audit log and its channel store:
			// a refused start must leave neither behind (the test below).
			for _, kept := range startedFiles {
				assert.FileExists(t, filepath.Join(p.dataDir, kept))
			}
		})
	}
}

// startedFiles are what a start that got past the cost config leaves in its
// data folder: the audit log and the channel store.
var startedFiles = []string{"audit.jsonl", "channels.db"}

// TestOrchestratorProcess_RefusesToStartWithoutACostConfig: a cost config
// that does not load ends the start with exit status 1 and one line that
// names the file and the fault. Nothing is served first, and no audit log
// and no store is left behind.
//
// Before ISSUE-0204's fix the process logged a warning and ran on with no
// wallet: healthy to a probe, and of no use to any agent.
func TestOrchestratorProcess_RefusesToStartWithoutACostConfig(t *testing.T) {
	tests := []struct {
		name string
		// configDir builds the case's config folder.
		configDir func(t *testing.T) string
		wantFault string
	}{
		{
			name: "the shipped config with budgets misspelled",
			configDir: func(t *testing.T) string {
				dir := copyOfShippedConfig(t)
				path := filepath.Join(dir, "optimization.yaml")
				shipped, err := os.ReadFile(path) //nolint:gosec
				require.NoError(t, err)
				require.Equal(t, 1, strings.Count(string(shipped), "\n  budgets:\n"))
				broken := strings.Replace(string(shipped), "\n  budgets:\n", "\n  budgetz:\n", 1)
				require.NoError(t, os.WriteFile(path, []byte(broken), 0o644)) //nolint:gosec
				return dir
			},
			wantFault: "field budgetz not found",
		},
		{
			name: "the shipped config without its optimization.yaml",
			configDir: func(t *testing.T) string {
				dir := copyOfShippedConfig(t)
				require.NoError(t, os.Remove(filepath.Join(dir, "optimization.yaml")))
				return dir
			},
			wantFault: "read optimization config",
		},
		{
			name:      "an empty config folder",
			configDir: func(t *testing.T) string { return t.TempDir() },
			wantFault: "read optimization config",
		},
		{
			name: "a config folder whose only file sets a limit below zero",
			configDir: func(t *testing.T) string {
				return costConfigDir(t, budgets(-1, 10, 5))
			},
			wantFault: "validate cost config: global max_daily_usd must be",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			configDir := tt.configDir(t)
			p := startOrchestrator(t, configDir, filepath.Join(t.TempDir(), "logs"))

			exitStatus := p.waitExit()

			out := p.output.String()
			assert.Equal(t, 1, exitStatus, "a refused start is exit status 1\n%s", out)
			assert.Contains(t, out, "refusing to start: the cost config did not load")
			assert.Contains(t, out, filepath.Join(configDir, "optimization.yaml"), "the line names the file")
			assert.Contains(t, out, tt.wantFault, "the line names the fault")
			assert.NotContains(t, out, "HTTP server starting", "nothing is served before the refusal")
			_, err := net.DialTimeout("tcp", p.httpAddr, time.Second)
			var refused *net.OpError
			assert.True(t, errors.As(err, &refused), "nothing listens on the REST port afterwards: %v", err)
			for _, kept := range startedFiles {
				assert.NoFileExists(t, filepath.Join(p.dataDir, kept), "the refusal comes before this is opened")
			}
		})
	}
}

// TestOrchestratorProcess_RefusesToStartWhenTheAgentPortIsTaken: agents reach
// the wallet on one port, so an orchestrator that cannot open it does not
// start. It exits with status 1 and a line that names the address.
func TestOrchestratorProcess_RefusesToStartWhenTheAgentPortIsTaken(t *testing.T) {
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	defer taken.Close() //nolint:errcheck
	port := taken.Addr().(*net.TCPAddr).Port

	p := startOrchestratorOn(t, shippedConfig(t), filepath.Join(t.TempDir(), "logs"), port)

	exitStatus := p.waitExit()

	out := p.output.String()
	assert.Equal(t, 1, exitStatus, "a refused start is exit status 1\n%s", out)
	assert.Contains(t, out, "failed to listen on gRPC port")
	assert.Contains(t, out, taken.Addr().String(), "the line names the address")
	assert.NotContains(t, out, "HTTP server starting", "nothing is served before the refusal")
}
