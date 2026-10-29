package main

import (
	"sync"
	"testing"
	"time"
)

func TestStreamBreakerRejectsStaleSuccessAndAllowsOneProbe(t *testing.T) {
	p := &streamPool{}
	cfg := Settings{StreamFailureThreshold: 1, StreamCooldown: time.Second}
	workers := []WorkerDoc{{WorkerID: "a", Addr: "a:1"}}
	now := time.Now()
	first, _ := p.acquire(workers, 0, nil, cfg, now)
	old, _ := p.acquire(workers, 0, nil, cfg, now)
	p.release(first, -1, cfg, now)
	p.release(old, 1, cfg, now) // A concurrent success must not erase newer failure.
	if _, ok := p.acquire(workers, 0, nil, cfg, now); ok {
		t.Fatal("open worker selected")
	}
	var wg sync.WaitGroup
	var mu sync.Mutex
	var probes []streamLease
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			lease, ok := p.acquire(workers, 0, nil, cfg, now.Add(time.Second))
			if ok {
				mu.Lock()
				probes = append(probes, lease)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if len(probes) != 1 {
		t.Fatalf("%d simultaneous probes", len(probes))
	}
	p.release(probes[0], 1, cfg, now.Add(time.Second))
	lease, ok := p.acquire(workers, 0, nil, cfg, now.Add(time.Second))
	if !ok || lease.probe {
		t.Fatal("successful probe did not restore worker")
	}
	p.release(lease, 1, cfg, now)
	if p.workers[streamKey(workers[0])].inflight != 0 {
		t.Fatal("reservation leaked")
	}
}

func TestStreamClientFailureDoesNotOpenBreakerAndRetriesAvoidBusyWorker(t *testing.T) {
	p := &streamPool{}
	cfg := Settings{StreamFailureThreshold: 1, StreamCooldown: time.Second, StreamLeastInflightRetries: true}
	w := []WorkerDoc{{WorkerID: "a", Addr: "a:1"}, {WorkerID: "b", Addr: "b:1"}, {WorkerID: "c", Addr: "c:1"}}
	now := time.Now()
	client, _ := p.acquire(w, 0, nil, cfg, now)
	if p.release(client, 0, cfg, now) {
		t.Fatal("client fault quarantined upstream")
	}
	busy, _ := p.acquire(w, 1, nil, cfg, now)
	retry, ok := p.acquire(w, 0, map[string]bool{streamKey(w[0]): true}, cfg, now)
	if !ok || retry.worker.WorkerID != "c" {
		t.Fatal("retry did not avoid busy worker")
	}
	p.release(busy, 1, cfg, now)
	p.release(retry, 1, cfg, now)
	// A new address is new health state; retired idle entries are pruned.
	p.acquire([]WorkerDoc{{WorkerID: "a", Addr: "new:1"}}, 0, nil, cfg, now)
	if len(p.workers) != 1 {
		t.Fatal("retired worker health retained")
	}
}
