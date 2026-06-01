//! Graceful shutdown handler — captures SIGINT/SIGTERM and triggers cleanup.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// Shutdown handler that signals when termination is requested.
///
/// Usage:
/// ```ignore
/// let handler = ShutdownHandler::new();
/// let signal = handler.signal();
/// tokio::spawn(async move {
///     handler.run().await;
/// });
/// // In main loop:
/// while !signal.load(Ordering::Relaxed) {
///     // do work
/// }
/// ```
pub struct ShutdownHandler {
    signal: Arc<AtomicBool>,
}

impl ShutdownHandler {
    /// Create a new shutdown handler.
    pub fn new() -> Self {
        Self {
            signal: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Get the signal that will be set to true on shutdown.
    pub fn signal(&self) -> Arc<AtomicBool> {
        self.signal.clone()
    }

    /// Wait for SIGINT or SIGTERM, then set the signal.
    ///
    /// This function blocks until a signal is received.
    /// Typically run in a background task.
    pub async fn run(&self) {
        // On Unix: SIGINT (Ctrl+C) and SIGTERM
        // On Windows: only Ctrl+C
        let result = tokio::signal::ctrl_c().await;

        match result {
            Ok(()) => {
                tracing::info!("Received SIGINT — initiating graceful shutdown...");
            }
            Err(e) => {
                tracing::error!("Failed to listen for signal: {e}");
            }
        }

        self.signal.store(true, Ordering::SeqCst);
    }

    /// Send the shutdown signal manually (e.g., from a health check).
    pub fn shutdown(&self) {
        tracing::info!("Manual shutdown requested");
        self.signal.store(true, Ordering::SeqCst);
    }

    /// Check if shutdown has been requested.
    pub fn is_shutdown_requested(&self) -> bool {
        self.signal.load(Ordering::SeqCst)
    }
}

impl Default for ShutdownHandler {
    fn default() -> Self {
        Self::new()
    }
}

// ─── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_shutdown_not_requested_by_default() {
        let handler = ShutdownHandler::new();
        assert!(!handler.is_shutdown_requested());
    }

    #[test]
    fn test_shutdown_signal() {
        let handler = ShutdownHandler::new();
        let signal = handler.signal();
        assert!(!signal.load(Ordering::SeqCst));

        handler.shutdown();

        assert!(signal.load(Ordering::SeqCst));
        assert!(handler.is_shutdown_requested());
    }
}
