// Package gen holds the canonical deterministic data generator for Ledgerline.
//
// Determinism contract (spec/db-access.md "Seeder contract"): every row is a
// pure function of (uint64 seed, entity id). Generation
// order, goroutine scheduling, and chunk boundaries never affect any row's
// content. This lets the loader regenerate the same stream multiple times
// (multi-pass streaming) and parallelize freely while staying byte-stable.
package gen

import (
	"encoding/binary"
	"math/rand/v2"
)

// SeedBytes derives the 32-byte ChaCha8 key from a uint64 seed.
//
// Rule (frozen, documented in seed/README.md): write the uint64 in
// little-endian into an 8-byte block, then repeat that block 4 times to fill
// the [32]byte key. This is fully specified so a second language can reproduce
// the identical key if it ever needs to.
func SeedBytes(seed uint64) [32]byte {
	var key [32]byte
	var block [8]byte
	binary.LittleEndian.PutUint64(block[:], seed)
	for i := 0; i < 4; i++ {
		copy(key[i*8:(i+1)*8], block[:])
	}
	return key
}

// rootSource builds the root ChaCha8 source from a uint64 seed.
func rootSource(seed uint64) *rand.ChaCha8 {
	key := SeedBytes(seed)
	return rand.NewChaCha8(key)
}

// childRand returns a per-entity deterministic *rand.Rand keyed by (seed, kind,
// id). The child is a function of the inputs ONLY, so customer 7's stream is
// identical regardless of which goroutine generates it or in what order.
//
// Construction: a SplitMix64-style avalanche of (seed, kind, id) produces two
// 64-bit words that key a fresh ChaCha8. ChaCha8 needs a [32]byte key; we fill
// it from the two mixed words (each repeated, little-endian). Distinct (kind,id)
// pairs map to distinct keys with overwhelming probability, which is all the
// per-entity independence the seeder needs.
func childRand(seed uint64, kind uint64, id uint64) *rand.Rand {
	a := splitmix64(seed ^ mix(kind) ^ mix(id*0x9E3779B97F4A7C15))
	b := splitmix64(a ^ mix(id) ^ mix(kind*0xD1B54A32D192ED03))
	var key [32]byte
	binary.LittleEndian.PutUint64(key[0:8], a)
	binary.LittleEndian.PutUint64(key[8:16], b)
	binary.LittleEndian.PutUint64(key[16:24], a^0xFFFFFFFFFFFFFFFF)
	binary.LittleEndian.PutUint64(key[24:32], b^0xFFFFFFFFFFFFFFFF)
	return rand.New(rand.NewChaCha8(key))
}

// Entity-stream kinds (stable constants; never reorder/reuse a value).
const (
	kindCustomer uint64 = 1
	kindInvoice  uint64 = 2
)

// mix is one round of the SplitMix64 finalizer; used to scramble inputs before
// combining. Deterministic and platform-independent (pure integer ops).
func mix(x uint64) uint64 {
	x ^= x >> 30
	x *= 0xBF58476D1CE4E5B9
	x ^= x >> 27
	x *= 0x94D049BB133111EB
	x ^= x >> 31
	return x
}

// splitmix64 is the SplitMix64 step + finalizer applied to a state word.
func splitmix64(x uint64) uint64 {
	x += 0x9E3779B97F4A7C15
	return mix(x)
}
