/// Market regime detector — classifies into 4 regimes using statistical features.
///
/// Replaces numpy/scipy with pure Rust math for:
/// - Standard deviation, skewness, kurtosis
/// - Volatility percentile ranking
/// - Regime classification logic

/// Market regime classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarketRegime {
    CalmTrending,
    VolatileTrending,
    MeanReverting,
    Crash,
}

/// Immutable regime detection result.
#[derive(Debug, Clone)]
pub struct RegimeResult {
    pub regime: MarketRegime,
    pub confidence: f64,
    pub volatility_percentile: f64,
    pub skewness: f64,
    pub kurtosis_val: f64,
    pub energy: f64,
}

/// Detects market regime using statistical features.
pub struct RegimeDetector {
    lookback: usize,
    min_samples: usize,
    returns: Vec<f64>,
    vol_history: Vec<f64>,
}

impl RegimeDetector {
    pub fn new(lookback: usize) -> Self {
        Self {
            lookback,
            min_samples: 30,
            returns: Vec::new(),
            vol_history: Vec::new(),
        }
    }

    /// Record a new return observation.
    pub fn update(&mut self, return_value: f64) {
        self.returns.push(return_value);
        if self.returns.len() > self.lookback * 2 {
            self.returns = self.returns.split_off(self.returns.len() - self.lookback * 2);
        }
    }

    /// Classify the current market regime.
    pub fn detect(&mut self) -> RegimeResult {
        let returns = if self.returns.len() >= self.lookback {
            self.returns[self.returns.len() - self.lookback..].to_vec()
        } else {
            self.returns.clone()
        };

        if returns.len() < self.min_samples {
            return RegimeResult {
                regime: MarketRegime::MeanReverting,
                confidence: 0.0,
                volatility_percentile: 0.5,
                skewness: 0.0,
                kurtosis_val: 0.0,
                energy: 1.0,
            };
        }

        let features = compute_features(&returns);

        // Track vol percentile
        self.vol_history.push(features.vol);
        if self.vol_history.len() > self.lookback {
            self.vol_history = self.vol_history.split_off(self.vol_history.len() - self.lookback);
        }
        let vol_percentile = compute_vol_percentile(features.vol, &self.vol_history);

        let (regime, confidence) = classify_regime(&features, vol_percentile);
        let energy = compute_energy(confidence, vol_percentile);

        RegimeResult {
            regime,
            confidence,
            volatility_percentile: vol_percentile,
            skewness: features.skew,
            kurtosis_val: features.kurt,
            energy,
        }
    }
}

struct Features {
    vol: f64,
    skew: f64,
    kurt: f64,
    mean: f64,
}

fn compute_features(returns: &[f64]) -> Features {
    if returns.len() < 20 {
        return Features { vol: 0.0, skew: 0.0, kurt: 0.0, mean: 0.0 };
    }

    let n = returns.len() as f64;
    let mean = returns.iter().sum::<f64>() / n;
    let variance = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / n;
    let vol = variance.sqrt();

    // Skewness (bias-corrected)
    let m3 = returns.iter().map(|r| (r - mean).powi(3)).sum::<f64>() / n;
    let skew = if variance > 0.0 {
        m3 / (variance.powf(1.5)) * ((n * (n - 1.0)).sqrt() / (n - 2.0))
    } else {
        0.0
    };

    // Excess kurtosis (Fisher, bias-corrected)
    let m4 = returns.iter().map(|r| (r - mean).powi(4)).sum::<f64>() / n;
    let kurt = if variance > 0.0 {
        let k = m4 / variance.powi(2) - 3.0;
        // Simple bias correction
        k * (n - 1.0) * (n - 1.0) / ((n - 2.0) * (n - 3.0))
    } else {
        0.0
    };

    Features { vol, skew, kurt, mean }
}

fn classify_regime(features: &Features, vol_percentile: f64) -> (MarketRegime, f64) {
    // Crash detection
    let is_crash = features.skew < -1.0
        || (features.mean < -0.015 && features.vol > 0.02);

    if is_crash {
        let confidence = if features.skew < -1.0 {
            (features.skew.abs() / 3.0).min(1.0)
        } else {
            (features.mean.abs() / 0.05).min(1.0)
        };
        return (MarketRegime::Crash, confidence);
    }

    let has_direction = if features.vol > 0.0 {
        features.mean.abs() > 0.3 * features.vol
    } else {
        false
    };

    if has_direction {
        if vol_percentile > 0.75 {
            let confidence = (0.6 + 0.4 * vol_percentile).min(1.0);
            return (MarketRegime::VolatileTrending, confidence);
        }
        let confidence = (0.6 + 0.4 * (1.0 - vol_percentile)).min(1.0);
        return (MarketRegime::CalmTrending, confidence);
    }

    let confidence = (0.5 + 0.3 * (1.0 - vol_percentile)).min(1.0);
    (MarketRegime::MeanReverting, confidence)
}

fn compute_energy(confidence: f64, vol_percentile: f64) -> f64 {
    (1.0 - confidence) * 0.6 + vol_percentile * 0.4
}

fn compute_vol_percentile(current_vol: f64, vol_history: &[f64]) -> f64 {
    if vol_history.is_empty() {
        return 0.5;
    }
    let below = vol_history.iter().filter(|&&v| v < current_vol).count() as f64;
    below / vol_history.len() as f64
}
