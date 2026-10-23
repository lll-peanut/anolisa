use sha2::{Digest, Sha256};
use std::hint::black_box;
use std::time::Instant;

fn main() {
    let input = vec![b'x'; 1024 * 1024];
    let mut result = Sha256::digest(black_box(&input));
    for _ in 0..32 {
        result = black_box(Sha256::digest(black_box(&input)));
    }
    let started = Instant::now();
    for _ in 0..256 {
        for _ in 0..4 {
            result = black_box(Sha256::digest(black_box(&input)));
        }
    }
    println!(
        "{{\"four_hashes_ms\":{},\"digest\":\"{:x}\"}}",
        started.elapsed().as_secs_f64() * 1000.0 / 256.0,
        black_box(result)
    );
}
