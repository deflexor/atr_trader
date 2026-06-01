//! Strategy implementations — portfolio-level strategies that operate on a universe of assets.
//!
//! Unlike the per-symbol signal generators in [`crate::signals`], these strategies
//! rank and allocate across multiple symbols simultaneously.

pub mod funding_arb;

pub use funding_arb::*;
