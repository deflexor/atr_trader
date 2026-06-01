//! Production utilities — config validation and graceful shutdown.

pub mod shutdown;
pub mod validator;

pub use shutdown::ShutdownHandler;
pub use validator::{validate_config, print_validation};
