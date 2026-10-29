package main

import (
	"bufio"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func TestStreamingTimesOutputAndCompletion(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.(http.Flusher).Flush()
		time.Sleep(30 * time.Millisecond)
		io.WriteString(w, "data: {\"choices\":[{\"text\":\"a\"}]}\n\n")
		w.(http.Flusher).Flush()
		time.Sleep(40 * time.Millisecond)
		io.WriteString(w, "data: {\"choices\":[{\"text\":\"b\"}]}\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()
	cfg := config{url: server.URL, maxTokens: 2, timeout: time.Second}
	row := streamRequest(server.Client(), cfg, streamJob{"id", "prompt", 0}, time.Now())
	if !row.Complete || row.Events != 2 || row.TTFT == nil || *row.TTFT < 25 || row.Latency < 65 || len(row.Gaps) != 1 || row.Gaps[0] < 35 {
		t.Fatalf("incorrect stream timing: %+v", row)
	}
}
func TestTruncatedStreamIsNotSuccess(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "data: {\"choices\":[{\"text\":\"a\"}]}\n\n")
	}))
	defer server.Close()
	row := streamRequest(server.Client(), config{url: server.URL, maxTokens: 2}, streamJob{"id", "prompt", 0}, time.Now())
	if row.Complete || row.Error == "" {
		t.Fatalf("truncated stream succeeded: %+v", row)
	}
}
func TestSSEParsesEventsNotReadChunks(t *testing.T) {
	reader := bufio.NewReader(strings.NewReader(": heartbeat\r\n\r\ndata: {\"a\":\r\ndata: 1}\r\n\r\ndata: [DONE]\n\n"))
	first, err := nextSSE(reader)
	if first != "" || err != nil {
		t.Fatal(first, err)
	}
	second, err := nextSSE(reader)
	if second != "{\"a\":\n1}" || err != nil {
		t.Fatal(second, err)
	}
	done, err := nextSSE(reader)
	if done != "[DONE]" || err != nil {
		t.Fatal(done, err)
	}
}
func TestBufferedLatencyIncludesBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.(http.Flusher).Flush()
		time.Sleep(40 * time.Millisecond)
		io.WriteString(w, `{"usage":{"completion_tokens":1}}`)
	}))
	defer server.Close()
	row := doRequest(server.Client(), config{url: server.URL}, "x")
	if row.failed || row.latencyMs < 35 {
		t.Fatalf("timer stopped before body: %+v", row)
	}
}

func TestStreamingCapacityAccountsForEveryOfferedRequest(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(120 * time.Millisecond)
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "data: {\"choices\":[{\"text\":\"x\"}]}\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()
	path := t.TempDir() + "/requests.jsonl"
	code := runStreaming(config{url: server.URL, concurrency: 1, rps: 100, duration: 100 * time.Millisecond,
		timeout: time.Second, maxTokens: 1, requestsOut: path, requestPrefix: "capacity"})
	if code != 1 {
		t.Fatalf("over-capacity experiment exit=%d, want failure", code)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	completed, dropped := 0, 0
	for _, line := range lines {
		var row streamResult
		if err := json.Unmarshal([]byte(line), &row); err != nil {
			t.Fatal(err)
		}
		if row.Complete {
			completed++
		}
		if row.Dropped {
			dropped++
		}
	}
	if len(lines) != 10 || completed != 1 || dropped != 9 {
		t.Fatalf("offered=%d complete=%d drop=%d", len(lines), completed, dropped)
	}
}

func TestCancellationAppliesOnlyToSelectedDispatchWindow(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for i := 0; i < 4; i++ {
			io.WriteString(w, "data: {\"choices\":[{\"text\":\"x\"}]}\n\n")
		}
		io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer server.Close()
	cfg := config{url: server.URL, maxTokens: 4, cancelAfterEvents: 2, cancelWindowStart: 1, cancelWindowEnd: 3}
	baseline := streamRequest(server.Client(), cfg, streamJob{"healthy", "x", 0}, time.Now())
	canceled := streamRequest(server.Client(), cfg, streamJob{"fault", "x", 0}, time.Now().Add(-2*time.Second))
	if !baseline.Complete || baseline.Events != 4 {
		t.Fatalf("healthy request canceled: %+v", baseline)
	}
	if canceled.Complete || canceled.Error != "client_canceled" || canceled.Events != 2 {
		t.Fatalf("fault request not canceled: %+v", canceled)
	}
}
