"""Small supervised logistic model for the deployment-risk demo.

The bundled training cases are illustrative. Calibrate this model against real
release telemetry before using its scores to make production decisions.
"""

import math


# Each row is normalized: CPU, memory, p95 latency, error rate, HTTP 5xx
# rate, unsuccessful request rate, and health-check failure.
TRAINING_CASES = (
    ((0.05, 0.25, 0.03, 0.00, 0.00, 0.00, 0.0), 0),
    ((0.18, 0.40, 0.10, 0.005, 0.00, 0.005, 0.0), 0),
    ((0.30, 0.50, 0.12, 0.01, 0.002, 0.01, 0.0), 0),
    ((0.50, 0.60, 0.20, 0.02, 0.005, 0.02, 0.0), 0),
    ((0.65, 0.70, 0.25, 0.03, 0.01, 0.03, 0.0), 0),
    ((0.40, 0.40, 0.15, 0.01, 0.005, 0.01, 0.0), 0),
    ((0.95, 0.95, 0.05, 0.00, 0.00, 0.00, 0.0), 1),
    ((0.20, 0.95, 0.10, 0.00, 0.00, 0.00, 0.0), 1),
    ((0.85, 0.80, 0.50, 0.08, 0.05, 0.08, 0.0), 1),
    ((0.95, 0.95, 0.90, 0.20, 0.15, 0.20, 0.0), 1),
    ((0.30, 0.40, 0.10, 0.15, 0.12, 0.15, 0.0), 1),
    ((0.45, 0.55, 0.25, 0.03, 0.02, 0.03, 1.0), 1),
    ((0.30, 0.40, 0.10, 0.00, 0.00, 0.00, 1.0), 1),
    ((0.10, 0.30, 0.05, 0.00, 0.00, 0.00, 1.0), 1),
    ((0.60, 0.50, 0.10, 0.00, 0.00, 0.00, 1.0), 1),
    ((0.75, 0.90, 0.35, 0.02, 0.01, 0.02, 0.0), 1),
    ((0.90, 0.50, 0.40, 0.06, 0.04, 0.06, 0.0), 1),
)


def _sigmoid(value):
    value = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _fit_logistic_model():
    feature_count = len(TRAINING_CASES[0][0])
    weights = [0.0] * feature_count
    bias = 0.0
    learning_rate = 0.35
    regularization = 0.015
    row_count = len(TRAINING_CASES)

    for _ in range(1800):
        gradients = [0.0] * feature_count
        bias_gradient = 0.0
        for features, label in TRAINING_CASES:
            probability = _sigmoid(
                bias + sum(weight * value for weight, value in zip(weights, features))
            )
            error = probability - label
            bias_gradient += error
            for index, value in enumerate(features):
                gradients[index] += error * value
        bias -= learning_rate * bias_gradient / row_count
        weights = [
            weight - learning_rate * (gradient / row_count + regularization * weight)
            for weight, gradient in zip(weights, gradients)
        ]
    return bias, weights


_MODEL_BIAS, _MODEL_WEIGHTS = _fit_logistic_model()


def _normalize(metrics):
    def fraction(key):
        value = metrics.get(key)
        if value is None:
            return 0.0
        return max(0.0, min(1.0, float(value)))

    cpu = metrics.get("cpu_percent")
    memory = metrics.get("memory_percent")
    latency = metrics.get("p95_latency_ms")
    return (
        max(0.0, min(1.0, float(cpu or 0.0) / 100.0)),
        max(0.0, min(1.0, float(memory or 0.0) / 100.0)),
        max(0.0, min(1.0, float(latency or 0.0) / 1000.0)),
        fraction("error_rate"),
        fraction("http_5xx_rate"),
        1.0 - fraction("success_rate"),
        0.0 if metrics.get("health_ok", True) else 1.0,
    )


def predict_deployment_risk(metrics):
    """Return a trained logistic probability and readable risk band."""
    features = _normalize(metrics)
    probability = _sigmoid(
        _MODEL_BIAS
        + sum(weight * value for weight, value in zip(_MODEL_WEIGHTS, features))
    )
    percent = round(probability * 100)
    level = "LOW" if probability < 0.35 else "MEDIUM" if probability < 0.65 else "HIGH"
    return {
        "probability": probability,
        "risk_percent": percent,
        "risk_level": level,
    }
