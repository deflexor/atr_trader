"""Machine learning for signal prediction."""

from .features import FeatureEngine
from .model import (
    SignalClassifier,
    ModelConfig,
    create_classification_labels,
    class_to_direction,
    CLASS_UP,
    CLASS_DOWN,
)

# NOTE: TrainingPipeline is deprecated - use SignalClassifier directly
# Keeping the import here breaks the import chain due to SignalPredictor removal

__all__ = [
    "FeatureEngine",
    "SignalClassifier",
    "ModelConfig",
    "create_classification_labels",
    "class_to_direction",
    "CLASS_UP",
    "CLASS_DOWN",
]
