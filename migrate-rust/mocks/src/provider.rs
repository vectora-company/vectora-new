//! Fakes for the provider routing readiness probe.

use crate::AsyncScript;

/// Capabilities and health are separate operations and can fail independently.
/// The caller supplies the actual capability and error types of its contract.
pub struct ProviderProbe<C, E> {
    capabilities: AsyncScript<(), Result<C, E>>,
    health: AsyncScript<(), Result<bool, E>>,
}

impl<C: Clone, E: Clone> ProviderProbe<C, E> {
    pub fn new(
        capabilities: AsyncScript<(), Result<C, E>>,
        health: AsyncScript<(), Result<bool, E>>,
    ) -> Self {
        Self {
            capabilities,
            health,
        }
    }

    pub fn capabilities(&self) -> impl Future<Output = Result<C, E>> + '_ {
        self.capabilities.call(())
    }

    pub fn health(&self) -> impl Future<Output = Result<bool, E>> + '_ {
        self.health.call(())
    }

    pub fn capabilities_awaited(&self) -> usize {
        self.capabilities.awaits().len()
    }

    pub fn health_awaited(&self) -> usize {
        self.health.awaits().len()
    }
}
