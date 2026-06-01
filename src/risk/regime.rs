//! Market regime detection from statistical return features.
//!
//! Classifies market into 4 regimes: CALM_TRENDING, VOLATILE_TRENDING,
//! MEAN_REVERTING, CRASH. Uses rolling window of returns and statistical
//! features (volatility, skewness, kurtosis, mean) — no GMM needed.

use std::collections::VecDeque;

/// Market regime classification.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum MarketRegime {
    CalmTrending,
    VolatileTrending,
    MeanReverting,
    Crash,
}

/// Regime detection result.
#[derive(Debug, Clone)]
pub struct RegimeResult {
    pub regime: MarketRegime,
    pub confidence: f64,
    pub volatility_percentile: f64,
    pub skewness: f64,
    pub energy: f64, // 0=confident, 1=uncertain
}

/// Stateful regime detector.
///
/// Maintains a rolling window of returns and classifies the current regime.
/// Pure computation functions are separate for testability.
pub struct RegimeDetector {
    lookback: usize,
    min_samples: usize,
    returns: VecDeque<f64>,
    vol_history: VecDeque<f64>,
}

impl RegimeDetector {
    pub fn new(lookback: usize, min_samples: usize) -> Self {
        Self {
            lookback,
            min_samples,
            returns: VecDeque::with_capacity(lookback * 2),
            vol_history: VecDeque::with_capacity(lookback),
        }
    }

    pub fn record_return(&mut self, return_value: f64) {
        self.returns.push_back(return_value);
        if self.returns.len() > self.lookback * 2 {
            self.returns.pop_front();
        }
    }

    /// Detect current regime from recent returns.
    pub fn detect(&mut self) -> RegimeResult {
        let recent: Vec<f64> = self
            .returns
            .iter()
            .rev()
            .take(self.lookback)
            .copied()
            .collect();

        if recent.len() < self.min_samples {
            return RegimeResult {
                regime: MarketRegime::MeanReverting,
                confidence: 0.0,
                volatility_percentile: 0.5,
                skewness: 0.0,
                energy: 1.0,
            };
        }

        let features = compute_features(&recent);
        let vol = features.vol;

        // Track vol history for percentile
        self.vol_history.push_back(vol);
        if self.vol_history.len() > self.lookback {
            self.vol_history.pop_front();
        }

        let vol_percentile = compute_vol_percentile(vol, &self.vol_history);
        let (regime, confidence) = classify_regime(&features, vol_percentile);
        let energy = compute_energy(confidence, vol_percentile);

        RegimeResult {
            regime,
            confidence,
            volatility_percentile: vol_percentile,
            skewness: features.skew,
            energy,
        }
    }

    pub fn reset(&mut self) {
        self.returns.clear();
        self.vol_history.clear();
    }
}

impl Default for RegimeDetector {
    fn default() -> Self {
        Self::new(100, 30)
    }
}

/// Statistical features extracted from a return series.
#[derive(Debug, Clone)]
pub struct ReturnFeatures {
    pub vol: f64,
    pub skew: f64,
    pub kurt: f64,
    pub mean: f64,
}

/// Compute statistical features from return series. Pure function.
pub fn compute_features(returns: &[f64]) -> ReturnFeatures {
    let n = returns.len();
    if n < 2 {
        return ReturnFeatures {
            vol: 0.0,
            skew: 0.0,
            kurt: 0.0,
            mean: returns.first().copied().unwrap_or(0.0),
        };
    }

    let mean = returns.iter().sum::<f64>() / n as f64;

    // Variance and standard deviation (population)
    let variance = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / n as f64;
    let vol = variance.sqrt();

    if vol == 0.0 {
        return ReturnFeatures {
            vol: 0.0,
            skew: 0.0,
            kurt: 0.0,
            mean,
        };
    }

    // Skewness (adjusted)
    let skew = if n > 2 {
        let m3 = returns.iter().map(|r| (r - mean).powi(3)).sum::<f64>() / n as f64;
        m3 / (vol * variance)
    } else {
        0.0
    };

    // Kurtosis (excess, Fisher = subtract 3)
    let kurt = if n > 3 {
        let m4 = returns.iter().map(|r| (r - mean).powi(4)).sum::<f64>() / n as f64;
        (m4 / (variance * variance)) - 3.0
    } else {
        0.0
    };

    ReturnFeatures {
        vol,
        skew,
        kurt,
        mean,
    }
}

/// Classify regime from features. Pure function.
pub fn classify_regime(
    features: &ReturnFeatures,
    vol_percentile: f64,
) -> (MarketRegime, f64) {
    let crash_skew_threshold = -1.0;
    let crash_mean_threshold = -0.015;
    let vol_high_percentile = 0.75;

    // Crash: extreme negative skew or strong negative drift + high vol
    if features.skew < crash_skew_threshold {
        let confidence = (features.skew.abs() / 3.0).min(1.0);
        return (MarketRegime::Crash, confidence);
    }
    if features.mean < crash_mean_threshold && features.vol > 0.02 {
        let confidence = (features.mean.abs() / 0.05).min(1.0);
        return (MarketRegime::Crash, confidence);
    }

    // Directional: mean drift significant relative to vol
    let has_direction = if features.vol > 0.0 {
        features.mean.abs() > 0.3 * features.vol
    } else {
        false
    };

    if has_direction {
        if vol_percentile > vol_high_percentile {
            let confidence = 0.6 + 0.4 * vol_percentile;
            return (MarketRegime::VolatileTrending, confidence.min(1.0));
        }
        let confidence = 0.6 + 0.4 * (1.0 - vol_percentile);
        return (MarketRegime::CalmTrending, confidence.min(1.0));
    }

    // No direction — mean reverting
    let confidence = 0.5 + 0.3 * (1.0 - vol_percentile);
    (MarketRegime::MeanReverting, confidence.min(1.0))
}

/// Compute uncertainty energy for position sizing. Pure function.
pub fn compute_energy(confidence: f64, vol_percentile: f64) -> f64 {
    (1.0 - confidence) * 0.6 + vol_percentile * 0.4
}

/// Compute volatility percentile. Pure function.
pub fn compute_vol_percentile(current_vol: f64, vol_history: &VecDeque<f64>) -> f64 {
    if vol_history.is_empty() {
        return 0.5;
    }
    let below = vol_history.iter().filter(|&&v| v < current_vol).count();
    below as f64 / vol_history.len() as f64
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compute_features_basic() {
        let returns = vec![0.01, -0.005, 0.02, -0.01, 0.015];
        let f = compute_features(&returns);
        assert!(f.vol > 0.0, "Volatility should be positive");
        assert!(f.mean > 0.0, "Mean should be positive (mostly gains)");
    }

    #[test]
    fn test_regime_calm_trending() {
        // Positive drift, low vol
        let returns: Vec<f64> = (0..60).map(|i| 0.005 + (i as f64 * 0.0001).sin() * 0.001).collect();
        let features = compute_features(&returns);
        let mut vol_hist = VecDeque::new();
        vol_hist.push_back(features.vol);
        // Add some higher vol history so this one is low
        for _ in 0..10 {
            vol_hist.push_back(features.vol * 3.0);
        }
        let vp = compute_vol_percentile(features.vol, &vol_hist);
        let (regime, _) = classify_regime(&features, vp);
        assert_eq!(regime, MarketRegime::CalmTrending);
    }

    #[test]
    fn test_regime_crash_negative_skew() {
        let returns: Vec<f64> = vec![
            -0.05, -0.03, 0.01, -0.04, -0.06, -0.02, -0.08, -0.03, 0.02, -0.05,
            -0.04, -0.07, -0.01, -0.03, -0.05, -0.02, -0.06, -0.04, 0.01, -0.03,
            -0.05, -0.02, -0.07, -0.03, -0.04, -0.06, -0.02, -0.05, -0.03, -0.08,
        ];
        let features = compute_features(&returns);
        let mut vol_hist = VecDeque::new();
        vol_hist.push_back(features.vol);
        let vp = compute_vol_percentile(features.vol, &vol_hist);
        let (regime, _) = classify_regime(&features, vp);
        assert_eq!(regime, MarketRegime::Crash);
    }

    #[test]
    fn test_regime_mean_reverting() {
        // Alternating up/down — no direction
        let returns: Vec<f64> = (0..60).map(|i| if i % 2 == 0 { 0.01 } else { -0.01 }).collect();
        let features = compute_features(&returns);
        let mut vol_hist = VecDeque::new();
        for _ in 0..5 { vol_hist.push_back(features.vol); }
        let vp = compute_vol_percentile(features.vol, &vol_hist);
        let (regime, _) = classify_regime(&features, vp);
        assert_eq!(regime, MarketRegime::MeanReverting);
    }

    #[test]
    fn test_vol_percentile() {
        let mut hist = VecDeque::new();
        for v in &[1.0, 2.0, 3.0, 4.0, 5.0] {
            hist.push_back(*v);
        }
        assert!((compute_vol_percentile(3.0, &hist) - 0.4).abs() < 0.01);
        assert!((compute_vol_percentile(6.0, &hist) - 1.0).abs() < 0.01);
        assert!((compute_vol_percentile(0.5, &hist) - 0.0).abs() < 0.01);
    }

    #[test]
    fn test_energy() {
        let e = compute_energy(0.9, 0.2);
        assert!(e > 0.0);
        assert!(e < 1.0);
        // High confidence + low vol percentile → low energy
        assert!(e < 0.5);
    }

    #[test]
    fn test_insufficient_data() {
        let mut detector = RegimeDetector::new(100, 30);
        let r = detector.detect();
        assert_eq!(r.regime, MarketRegime::MeanReverting);
        assert_eq!(r.energy, 1.0);
    }
}
