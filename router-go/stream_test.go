package main

import (
	"bufio"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const outputFrame = "data: {\"choices\":[{\"text\":\"x\"}]}\n\n"

func streamWorker(handler http.HandlerFunc) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		handler(w, r)
	}))
}
func streamRouter(t *testing.T, upstreams ...*httptest.Server) *httptest.Server {
	t.Helper()
	var docs []WorkerDoc
	for i, up := range upstreams {
		docs = append(docs, WorkerDoc{WorkerID: string(rune('a' + i)), Addr: strings.TrimPrefix(up.URL, "http://")})
	}
	router := newTestServer(t, docs, nil)
	router.state.rr = -1 // Make the first selected worker explicit in failover tests.
	server := httptest.NewServer(router.Handler())
	t.Cleanup(server.Close)
	return server
}
func requestStream(t *testing.T, url string) *http.Response {
	t.Helper()
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Post(url+"/v1/completions", "application/json", strings.NewReader(`{"stream":true}`))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}
func TestStreamFlushesBeforeCompletion(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	worker := streamWorker(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, outputFrame)
		w.(http.Flusher).Flush()
		select {
		case <-release:
			io.WriteString(w, "data: [DONE]\n\n")
		case <-r.Context().Done():
		}
	})
	defer worker.Close()
	router := streamRouter(t, worker)
	resp := requestStream(t, router.URL)
	defer resp.Body.Close()
	line, err := bufio.NewReader(resp.Body).ReadString('\n')
	if err != nil || !strings.Contains(line, `"text":"x"`) {
		t.Fatalf("first output unavailable before completion: %q %v", line, err)
	}
}
func TestStreamRetriesBeforeOutput(t *testing.T) {
	bad := streamWorker(func(w http.ResponseWriter, r *http.Request) { http.Error(w, "retry", 503) })
	defer bad.Close()
	good := streamWorker(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, outputFrame+"data: [DONE]\n\n")
	})
	defer good.Close()
	router := streamRouter(t, bad, good)
	resp := requestStream(t, router.URL)
	data, err := io.ReadAll(resp.Body)
	if err != nil || resp.StatusCode != 200 || !strings.Contains(string(data), "[DONE]") {
		t.Fatalf("failover: %d %s %v", resp.StatusCode, data, err)
	}
}
func TestStreamNeverRetriesAfterOutput(t *testing.T) {
	bad := streamWorker(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, outputFrame)
		w.(http.Flusher).Flush()
	})
	defer bad.Close()
	var calls atomic.Int32
	good := streamWorker(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, outputFrame+"data: [DONE]\n\n")
	})
	defer good.Close()
	router := streamRouter(t, bad, good)
	resp := requestStream(t, router.URL)
	data, _ := io.ReadAll(resp.Body)
	if calls.Load() != 0 || strings.Contains(string(data), "[DONE]") {
		t.Fatalf("interrupted output retried: %d %s", calls.Load(), data)
	}
}
func TestStreamCancellationReachesWorker(t *testing.T) {
	canceled := make(chan struct{})
	worker := streamWorker(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, outputFrame)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(canceled)
	})
	defer worker.Close()
	router := streamRouter(t, worker)
	resp := requestStream(t, router.URL)
	bufio.NewReader(resp.Body).ReadString('\n')
	resp.Body.Close()
	select {
	case <-canceled:
	case <-time.After(time.Second):
		t.Fatal("upstream not canceled")
	}
}
func TestStreamRejectsInvalidFraming(t *testing.T) {
	for _, input := range []string{"data: invalid\n\n", "data: " + strings.Repeat("x", 70000) + "\n\n", "data: {}"} {
		frame := readStreamFrame(bufio.NewReaderSize(strings.NewReader(input), 65536))
		if frame.err == nil {
			t.Fatalf("accepted malformed/oversized frame")
		}
	}
	frame := readStreamFrame(bufio.NewReader(strings.NewReader(": heartbeat\n\n")))
	if frame.err != nil || frame.output || frame.done {
		t.Fatal("heartbeat counted as output")
	}
}
func TestStreamFirstOutputTimeoutIgnoresHeaders(t *testing.T) {
	bad := streamWorker(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	defer bad.Close()
	good := streamWorker(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, outputFrame+"data: [DONE]\n\n")
	})
	defer good.Close()
	docs := []WorkerDoc{{WorkerID: "bad", Addr: strings.TrimPrefix(bad.URL, "http://")}, {WorkerID: "good", Addr: strings.TrimPrefix(good.URL, "http://")}}
	server := newTestServer(t, docs, nil)
	server.state.rr = -1
	server.settings.FirstOutputTimeout = 50 * time.Millisecond
	router := httptest.NewServer(server.Handler())
	defer router.Close()
	resp := requestStream(t, router.URL)
	data, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(data), "[DONE]") {
		t.Fatalf("no first-output failover: %s", data)
	}
}
