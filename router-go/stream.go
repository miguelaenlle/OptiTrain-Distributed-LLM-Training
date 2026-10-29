package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

// A separate streaming branch leaves the buffered Poster contract intact.
func wantsStream(body []byte) bool {
	var request struct {
		Stream bool `json:"stream"`
	}
	return json.Unmarshal(body, &request) == nil && request.Stream
}

type streamFrame struct {
	wire         []byte
	output, done bool
	err          error
}

// Downstream failures must never mark a healthy worker unhealthy.
type downstreamStreamError struct{ error }

func readStreamFrame(reader *bufio.Reader) streamFrame {
	var wire, payload strings.Builder
	for {
		raw, err := reader.ReadSlice('\n')
		line := string(raw)
		if len(line) > 65536 || wire.Len()+len(line) > 262144 {
			return streamFrame{err: errors.New("SSE event exceeds limit")}
		}
		wire.WriteString(line)
		if err != nil {
			return streamFrame{err: err}
		}
		clean := strings.TrimRight(line, "\r\n")
		if clean == "" {
			break
		}
		if strings.HasPrefix(clean, "data:") {
			if payload.Len() > 0 {
				payload.WriteByte('\n')
			}
			payload.WriteString(strings.TrimPrefix(strings.TrimPrefix(clean, "data:"), " "))
		}
	}
	data := payload.String()
	frame := streamFrame{wire: []byte(wire.String())}
	if data == "" {
		return frame
	}
	if data == "[DONE]" {
		frame.done = true
		return frame
	}
	var event struct {
		Choices []struct {
			Text  string `json:"text"`
			Delta struct {
				Content string `json:"content"`
			} `json:"delta"`
		} `json:"choices"`
	}
	if err := json.Unmarshal([]byte(data), &event); err != nil {
		return streamFrame{err: fmt.Errorf("invalid SSE JSON: %w", err)}
	}
	for _, choice := range event.Choices {
		frame.output = frame.output || choice.Text != "" || choice.Delta.Content != ""
	}
	return frame
}
func (s *Server) handleStream(w http.ResponseWriter, r *http.Request, body []byte, workers []WorkerDoc, start int) RouteResult {
	result := RouteResult{StatusCode: 503, Detail: "no live workers"}
	maximum := s.settings.MaxAttempts
	if maximum <= 0 {
		maximum = DefaultMaxAttempts
	}
	tried := make(map[string]bool)
	for k := 0; k < len(workers) && k < maximum; k++ {
		if r.Context().Err() != nil {
			break
		}
		lease, ok := s.streamPool.acquire(workers, start, tried, s.settings, time.Now())
		if !ok {
			break
		}
		worker := lease.worker
		tried[lease.key] = true
		result.Attempts++
		committed, status, err := s.streamAttempt(w, r, body, worker)
		outcome := 1
		var downstream downstreamStreamError
		if r.Context().Err() != nil || errors.As(err, &downstream) {
			outcome = 0
		} else if err != nil || status >= 500 {
			outcome = -1
		}
		open := s.streamPool.release(lease, outcome, s.settings, time.Now())
		s.streamAudit(r, worker, result.Attempts, status, committed, open, err)
		if err != nil {
			s.metrics.RecordAttempt(worker.WorkerID, AttemptTransport)
			result.Detail = fmt.Sprintf("%s: %v", worker.WorkerID, err)
			if committed {
				return result
			}
			continue
		}
		s.metrics.RecordAttempt(worker.WorkerID, AttemptResult(status))
		if status >= 200 && status < 300 {
			result.StatusCode = status
			result.WorkerID = worker.WorkerID
			result.Rerouted = result.Attempts > 1
			return result
		}
		if status >= 400 && status < 500 {
			result.StatusCode = status
			return result
		}
		result.Detail = fmt.Sprintf("%s: upstream %d", worker.WorkerID, status)
	}
	writeDetail(w, 503, result.Detail)
	return result
}
func (s *Server) streamAttempt(w http.ResponseWriter, r *http.Request, body []byte, worker WorkerDoc) (bool, int, error) {
	timeout := s.settings.RequestTimeout
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	first := s.settings.FirstOutputTimeout
	if first <= 0 {
		first = timeout
	}
	idle := s.settings.StreamIdleTimeout
	if idle <= 0 {
		idle = timeout
	}
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()
	timer := time.AfterFunc(first, cancel)
	defer timer.Stop()
	req, err := http.NewRequestWithContext(ctx, "POST", "http://"+worker.Addr+"/v1/completions", strings.NewReader(string(body)))
	if err != nil {
		return false, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("X-Request-ID", r.Header.Get("X-Request-ID"))
	resp, err := s.streamClient.Do(req)
	if err != nil {
		return false, 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 && resp.StatusCode < 500 {
		data, e := io.ReadAll(io.LimitReader(resp.Body, 65537))
		if e != nil {
			return false, resp.StatusCode, e
		}
		if len(data) > 65536 {
			return false, resp.StatusCode, errors.New("error body too large")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(resp.StatusCode)
		_, err = w.Write(data)
		if err != nil {
			return true, resp.StatusCode, downstreamStreamError{err}
		}
		return true, resp.StatusCode, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false, resp.StatusCode, nil
	}
	if !strings.HasPrefix(strings.ToLower(resp.Header.Get("Content-Type")), "text/event-stream") {
		return false, 0, errors.New("expected text/event-stream")
	}
	frames := make(chan streamFrame, 1)
	go func() {
		reader := bufio.NewReaderSize(resp.Body, 65536)
		for {
			frame := readStreamFrame(reader)
			select {
			case frames <- frame:
			case <-ctx.Done():
				return
			}
			if frame.err != nil || frame.done {
				return
			}
		}
	}()
	committed := false
	var prelude []byte
	for {
		select {
		case <-ctx.Done():
			return committed, 0, ctx.Err()
		case frame := <-frames:
			if frame.err != nil {
				return committed, 0, fmt.Errorf("incomplete stream: %w", frame.err)
			}
			if !committed {
				prelude = append(prelude, frame.wire...)
				if len(prelude) > 262144 {
					return false, 0, errors.New("SSE prelude too large")
				}
				if !frame.output && !frame.done {
					continue
				}
				w.Header().Set("Content-Type", "text/event-stream")
				w.Header().Set("Cache-Control", "no-cache")
				w.WriteHeader(resp.StatusCode)
				committed = true
				frame.wire = prelude
				prelude = nil
			}
			_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(idle))
			if _, err = w.Write(frame.wire); err != nil {
				return committed, 0, downstreamStreamError{err}
			}
			if err = http.NewResponseController(w).Flush(); err != nil {
				return committed, 0, downstreamStreamError{err}
			}
			if frame.output {
				timer.Reset(idle)
			}
			if frame.done {
				return committed, resp.StatusCode, nil
			}
		}
	}
}
func (s *Server) streamAudit(r *http.Request, worker WorkerDoc, attempt, status int, committed, breakerOpen bool, err error) {
	path := os.Getenv("ROUTER_ATTEMPTS_FILE")
	if path == "" {
		return
	}
	record := map[string]any{"request_id": r.Header.Get("X-Request-ID"), "worker": worker.WorkerID, "attempt": attempt, "status": status, "committed": committed, "unix_ns": time.Now().UnixNano()}
	record["breaker_open"] = breakerOpen
	if err != nil {
		record["error"] = err.Error()
	}
	data, _ := json.Marshal(record)
	s.auditMu.Lock()
	defer s.auditMu.Unlock()
	file, e := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if e != nil {
		s.logger.Printf("attempt audit: %v", e)
		return
	}
	defer file.Close()
	if _, e = file.Write(append(data, '\n')); e != nil {
		s.logger.Printf("attempt audit: %v", e)
	}
}
