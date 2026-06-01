pub mod composite;
pub mod correlation;
pub mod regime;
pub mod velocity;

pub use composite::{compute_composite_score, CompositeRiskConfig, CompositeRiskScore};
pub use correlation::{CorrelationConfig, CorrelationMonitor, CorrelationRiskLevel, CorrelationSignal};
pub use regime::{MarketRegime, RegimeDetector, RegimeResult};
pub use velocity::{VelocityResult, VelocityTracker};
