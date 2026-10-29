package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// Each row is one offered logical request, including generator drops. Timings
// share the client's monotonic clock; wall timestamps are only correlation aids.
type streamResult struct {
	ID        string    `json:"request_id"`
	Scheduled float64   `json:"scheduled_s"`
	Dispatch  float64   `json:"dispatch_s"`
	UnixNS    int64     `json:"unix_ns"`
	TTFT      *float64  `json:"ttft_ms"`
	Gaps      []float64 `json:"gaps_ms"`
	Latency   float64   `json:"latency_ms"`
	Events    int       `json:"output_events"`
	Status    int       `json:"status"`
	Complete  bool      `json:"complete"`
	Error     string    `json:"error,omitempty"`
	Dropped   bool      `json:"generator_drop"`
}
type streamJob struct {
	id, prompt string
	scheduled  float64
}

func runStreaming(cfg config) int {
	file, err := os.OpenFile(cfg.requestsOut, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		fmt.Fprintln(os.Stderr, "loadgen requests:", err)
		return 2
	}
	defer file.Close()
	client := &http.Client{Timeout: cfg.timeout}
	// Reserve an in-flight slot before enqueueing. An unbuffered send can
	// falsely drop the first request merely because worker goroutines have not
	// reached their receive yet; this bounded handoff avoids that startup race.
	jobs := make(chan streamJob, cfg.concurrency)
	slots := make(chan struct{}, cfg.concurrency)
	for i := 0; i < cfg.concurrency; i++ {
		slots <- struct{}{}
	}
	rows := make(chan streamResult, cfg.concurrency)
	start := time.Now()
	if cfg.metaOut != "" {
		data, _ := json.Marshal(map[string]any{"start_unix_ns": start.UnixNano()})
		if err := os.WriteFile(cfg.metaOut, data, 0600); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 2
		}
	}
	var wg sync.WaitGroup
	for i := 0; i < cfg.concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobs {
				rows <- streamRequest(client, cfg, job, start)
				slots <- struct{}{}
			}
		}()
	}
	done := make(chan int, 1)
	go func() {
		encoder := json.NewEncoder(file)
		code := 0
		for row := range rows {
			if row.Error != "" || row.Dropped {
				if code == 0 {
					code = 1
				}
			}
			if err := encoder.Encode(row); err != nil {
				fmt.Fprintln(os.Stderr, "write request evidence:", err)
				code = 2
			}
		}
		done <- code
	}()
	prompts := loadPrompts(cfg.promptsFile)
	total := int(math.Ceil(cfg.duration.Seconds() * cfg.rps))
	for i := 0; i < total; i++ {
		scheduled := float64(i) / cfg.rps
		if scheduled >= cfg.duration.Seconds() {
			break
		}
		due := start.Add(time.Duration(scheduled * float64(time.Second)))
		if wait := time.Until(due); wait > 0 {
			time.Sleep(wait)
		}
		job := streamJob{fmt.Sprintf("%s-%d", cfg.requestPrefix, i), prompts[i%len(prompts)], scheduled}
		select {
		case <-slots:
			jobs <- job
		default:
			rows <- streamResult{ID: job.id, Scheduled: scheduled, Dispatch: time.Since(start).Seconds(), Dropped: true, Error: "generator_drop", Gaps: []float64{}}
		}
	}
	close(jobs)
	wg.Wait()
	close(rows)
	return <-done
}
func streamRequest(client *http.Client, cfg config, job streamJob, start time.Time) streamResult {
	began := time.Now()
	row := streamResult{ID: job.id, Scheduled: job.scheduled, Dispatch: began.Sub(start).Seconds(), UnixNS: began.UnixNano(), Gaps: []float64{}}
	body := map[string]any{"prompt": job.prompt, "max_tokens": cfg.maxTokens, "temperature": cfg.temperature, "stream": true}
	if cfg.model != "" {
		body["model"] = cfg.model
	}
	if cfg.inputTokens > 0 {
		body["replay_input_tokens"] = cfg.inputTokens
		body["request_id"] = job.id
	}
	payload, _ := json.Marshal(body)
	req, err := http.NewRequest("POST", cfg.url+"/v1/completions", bytes.NewReader(payload))
	if err != nil {
		row.Error = err.Error()
		return row
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Request-ID", job.id)
	resp, err := client.Do(req)
	if err != nil {
		row.Error = err.Error()
		row.Latency = float64(time.Since(began).Microseconds()) / 1000
		return row
	}
	defer resp.Body.Close()
	row.Status = resp.StatusCode
	finish := func() streamResult { row.Latency = float64(time.Since(began).Microseconds()) / 1000; return row }
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		row.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
		return finish()
	}
	if !strings.HasPrefix(strings.ToLower(resp.Header.Get("Content-Type")), "text/event-stream") {
		row.Error = "expected text/event-stream"
		return finish()
	}
	reader := bufio.NewReaderSize(resp.Body, 65536)
	previous := time.Time{}
	for {
		data, err := nextSSE(reader)
		if err != nil {
			row.Error = fmt.Sprintf("incomplete_stream: %v", err)
			return finish()
		}
		if data == "" {
			continue
		}
		if data == "[DONE]" {
			row.Complete = true
			return finish()
		}
		var event struct {
			Choices []struct {
				Text  string `json:"text"`
				Delta struct {
					Content string `json:"content"`
				} `json:"delta"`
			} `json:"choices"`
		}
		if err = json.Unmarshal([]byte(data), &event); err != nil {
			row.Error = "invalid_sse_json"
			return finish()
		}
		output := false
		for _, choice := range event.Choices {
			output = output || choice.Text != "" || choice.Delta.Content != ""
		}
		if !output {
			continue
		}
		now := time.Now()
		row.Events++
		if previous.IsZero() {
			ttft := float64(now.Sub(began).Microseconds()) / 1000
			row.TTFT = &ttft
		} else {
			row.Gaps = append(row.Gaps, float64(now.Sub(previous).Microseconds())/1000)
		}
		previous = now
		if cfg.cancelAfterEvents > 0 && row.Events >= cfg.cancelAfterEvents && row.Dispatch >= cfg.cancelWindowStart && (cfg.cancelWindowEnd == 0 || row.Dispatch < cfg.cancelWindowEnd) {
			row.Error = "client_canceled"
			return finish()
		}
	}
}
func nextSSE(reader *bufio.Reader) (string, error) {
	var data strings.Builder
	size := 0
	for {
		line, err := reader.ReadSlice('\n')
		size += len(line)
		if size > 262144 {
			return "", errors.New("SSE event exceeds limit")
		}
		if err != nil {
			if err == io.EOF {
				return "", io.ErrUnexpectedEOF
			}
			return "", err
		}
		clean := strings.TrimRight(string(line), "\r\n")
		if clean == "" {
			return data.String(), nil
		}
		if strings.HasPrefix(clean, "data:") {
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.WriteString(strings.TrimPrefix(strings.TrimPrefix(clean, "data:"), " "))
		}
	}
}
