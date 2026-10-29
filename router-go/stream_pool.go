package main

import (
	"sync"
	"time"
)

// Stream health is separate from discovery and the buffered routing policy.
// Reservations make both retry load selection and half-open probes atomic.
type streamHealth struct {
	inflight, failures int
	epoch              uint64
	openUntil          time.Time
	probing            bool
}
type streamPool struct {
	mu         sync.Mutex
	workers    map[string]*streamHealth
	recovering bool
}
type streamLease struct {
	worker    WorkerDoc
	key       string
	epoch     uint64
	probe     bool
	emergency bool
}

func streamKey(w WorkerDoc) string { return w.WorkerID + "\x00" + w.Addr }

func (p *streamPool) acquire(workers []WorkerDoc, start int, tried map[string]bool, settings Settings, now time.Time) (streamLease, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.workers == nil {
		p.workers = make(map[string]*streamHealth)
	}
	live := make(map[string]bool, len(workers))
	for _, w := range workers {
		live[streamKey(w)] = true
	}
	for key, health := range p.workers {
		if !live[key] && health.inflight == 0 {
			delete(p.workers, key)
		}
	}
	var chosen streamLease
	var selected *streamHealth
	for k := 0; k < len(workers); k++ {
		w := workers[(start+k)%len(workers)]
		key := streamKey(w)
		if tried[key] {
			continue
		}
		h := p.workers[key]
		if h == nil {
			h = &streamHealth{}
			p.workers[key] = h
		}
		probe := settings.StreamFailureThreshold > 0 && !h.openUntil.IsZero()
		if probe && (now.Before(h.openUntil) || h.probing) {
			continue
		}
		if selected == nil || (len(tried) > 0 && settings.StreamLeastInflightRetries && h.inflight < selected.inflight) {
			chosen = streamLease{worker: w, key: key, epoch: h.epoch, probe: probe}
			selected = h
		}
		if len(tried) == 0 || !settings.StreamLeastInflightRetries {
			break
		}
	}
	if selected == nil && settings.StreamProbeWhenAllOpen && !p.recovering && len(workers) > 0 {
		allOpen := true
		for _, w := range workers {
			h := p.workers[streamKey(w)]
			if h == nil || h.openUntil.IsZero() || h.probing {
				allOpen = false
				break
			}
		}
		if allOpen {
			for _, w := range workers {
				key := streamKey(w)
				h := p.workers[key]
				if tried[key] || h.inflight != 0 {
					continue
				}
				if selected == nil || h.openUntil.Before(selected.openUntil) {
					selected = h
					chosen = streamLease{worker: w, key: key, epoch: h.epoch, probe: true, emergency: true}
				}
			}
			if selected != nil {
				p.recovering = true
			}
		}
	}
	if selected == nil {
		return streamLease{}, false
	}
	selected.inflight++
	if chosen.probe {
		selected.probing = true
	}
	return chosen, true
}

// outcome: -1 attributable upstream failure; 0 client/downstream cancellation;
// +1 responsive upstream. Older attempts cannot close a newly opened circuit.
func (p *streamPool) release(lease streamLease, outcome int, settings Settings, now time.Time) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	h := p.workers[lease.key]
	h.inflight--
	if lease.emergency {
		p.recovering = false
	}
	if h.epoch != lease.epoch || settings.StreamFailureThreshold <= 0 {
		return !h.openUntil.IsZero()
	}
	if lease.probe {
		h.probing = false
	}
	if outcome < 0 {
		h.failures++
		if h.failures >= settings.StreamFailureThreshold {
			h.openUntil = now.Add(settings.StreamCooldown)
			h.probing = false
			h.epoch++
		}
	} else if outcome > 0 {
		h.failures = 0
		h.openUntil = time.Time{}
	}
	return !h.openUntil.IsZero()
}
