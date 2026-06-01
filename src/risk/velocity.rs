//! Unrealized P&L velocity tracker.
//!
//! Tracks the rate of change (velocity) of unrealized P&L per position.
//! Velocity = linear regression slope over a rolling window.
//! Only negative velocity (losing positions) contributes to risk.

use std::collections::{HashMap, VecDeque};

/// Velocity computation result.
#[derive(Debug, Clone)]
pub struct VelocityResult {
    pub velocity: f64,
    pub acceleration: f64,
    pub window_size: usize,
    pub current_pnl_pct: f64,
}

/// Stateful velocity tracker per position.
pub struct VelocityTracker {
    window_candles: usize,
    min_samples: usize,
    /// position_id → deque of (candle_idx, pnl_pct)
    history: HashMap<u64, VecDeque<(i64, f64)>>,
}

impl VelocityTracker {
    pub fn new(window_candles: usize, min_samples: usize) -> Self {
        Self {
            window_candles,
            min_samples,
            history: HashMap::new(),
        }
    }

    /// Record an unrealized P&L observation for a position.
    pub fn update(&mut self, position_id: u64, candle_idx: i64, pnl_pct: f64) {
        let entry = self
            .history
            .entry(position_id)
            .or_insert_with(|| VecDeque::with_capacity(self.window_candles));
        entry.push_back((candle_idx, pnl_pct));
        if entry.len() > self.window_candles {
            entry.pop_front();
        }
    }

    /// Compute velocity and acceleration for a position.
    /// Returns None if insufficient samples.
    pub fn compute(&self, position_id: u64) -> Option<VelocityResult> {
        let samples = self.history.get(&position_id)?;
        if samples.len() < self.min_samples {
            return None;
        }

        let items: Vec<(i64, f64)> = samples.iter().copied().collect();
        let n = items.len();

        let candle_indices: Vec<f64> = items.iter().map(|(i, _)| *i as f64).collect();
        let pnl_values: Vec<f64> = items.iter().map(|(_, p)| *p).collect();

        // Velocity = linear regression slope
        let velocity = regression_slope(&candle_indices, &pnl_values);
        let current_pnl = pnl_values[n - 1];

        // Acceleration: velocity difference between first and second half
        let acceleration = if n >= self.min_samples + 1 {
            let mid = n / 2;
            let v1 = regression_slope(&candle_indices[..=mid], &pnl_values[..=mid]);
            let v2 = regression_slope(&candle_indices[mid..], &pnl_values[mid..]);
            let dt = candle_indices[n - 1] - candle_indices[mid];
            if dt > 0.0 {
                (v2 - v1) / dt
            } else {
                0.0
            }
        } else {
            0.0
        };

        Some(VelocityResult {
            velocity,
            acceleration,
            window_size: n,
            current_pnl_pct: current_pnl,
        })
    }

    /// Remove tracking data for a closed position.
    pub fn remove(&mut self, position_id: u64) {
        self.history.remove(&position_id);
    }

    pub fn reset(&mut self) {
        self.history.clear();
    }

    pub fn tracked_positions(&self) -> usize {
        self.history.len()
    }
}

impl Default for VelocityTracker {
    fn default() -> Self {
        Self::new(5, 3)
    }
}

/// Compute linear regression slope. Pure function.
///
/// Returns 0.0 for degenerate cases (constant x, single point).
pub fn regression_slope(x: &[f64], y: &[f64]) -> f64 {
    let n = x.len();
    if n < 2 {
        return 0.0;
    }

    let mean_x = x.iter().sum::<f64>() / n as f64;
    let mean_y = y.iter().sum::<f64>() / n as f64;

    let numerator: f64 = x
        .iter()
        .zip(y.iter())
        .map(|(xi, yi)| (xi - mean_x) * (yi - mean_y))
        .sum();

    let denominator: f64 = x.iter().map(|xi| (xi - mean_x).powi(2)).sum();

    if denominator == 0.0 {
        return 0.0;
    }

    numerator / denominator
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_regression_slope() {
        // y = 2x + 1, slope should be 2
        let x = vec![0.0, 1.0, 2.0, 3.0, 4.0];
        let y = vec![1.0, 3.0, 5.0, 7.0, 9.0];
        let slope = regression_slope(&x, &y);
        assert!((slope - 2.0).abs() < 0.01, "Expected slope 2, got {slope}");
    }

    #[test]
    fn test_regression_slope_degenerate() {
        assert!((regression_slope(&[1.0], &[5.0]) - 0.0).abs() < 0.01);
        assert!((regression_slope(&[], &[]) - 0.0).abs() < 0.01);
    }

    #[test]
    fn test_velocity_negative() {
        let mut tracker = VelocityTracker::default();
        // Dropping P&L over 5 candles
        tracker.update(1, 0, 0.0);
        tracker.update(1, 1, -1.0);
        tracker.update(1, 2, -2.0);
        tracker.update(1, 3, -3.0);
        tracker.update(1, 4, -4.0);
        let result = tracker.compute(1);
        assert!(result.is_some());
        let r = result.unwrap();
        assert!(r.velocity < 0.0, "Velocity should be negative for losing position");
        assert!((r.current_pnl_pct - (-4.0)).abs() < 0.01);
    }

    #[test]
    fn test_velocity_insufficient() {
        let mut tracker = VelocityTracker::default();
        tracker.update(1, 0, 0.0);
        tracker.update(1, 1, -1.0);
        // Only 2 samples, need 3
        assert!(tracker.compute(1).is_none());
    }

    #[test]
    fn test_velocity_unknown_position() {
        let tracker = VelocityTracker::default();
        assert!(tracker.compute(999).is_none());
    }

    #[test]
    fn test_remove() {
        let mut tracker = VelocityTracker::default();
        tracker.update(1, 0, 0.0);
        tracker.update(1, 1, -1.0);
        tracker.update(1, 2, -2.0);
        assert_eq!(tracker.tracked_positions(), 1);
        tracker.remove(1);
        assert!(tracker.compute(1).is_none());
        assert_eq!(tracker.tracked_positions(), 0);
    }

    #[test]
    fn test_reset() {
        let mut tracker = VelocityTracker::default();
        tracker.update(1, 0, 0.0);
        tracker.update(1, 1, -1.0);
        tracker.update(1, 2, -2.0);
        tracker.reset();
        assert_eq!(tracker.tracked_positions(), 0);
    }
}
