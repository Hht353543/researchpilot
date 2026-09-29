"""Evaluation harness: golden dataset, deterministic judges, real metrics."""

from researchpilot.evaluation.dataset import GoldenTask, TaskCategory, dataset_stats, load_dataset
from researchpilot.evaluation.judge import TaskJudgement, judge_task
from researchpilot.evaluation.metrics import EvaluationMetrics, aggregate
from researchpilot.evaluation.runner import EvaluationReport, EvaluationRunner

__all__ = [
    "EvaluationMetrics",
    "EvaluationReport",
    "EvaluationRunner",
    "GoldenTask",
    "TaskCategory",
    "TaskJudgement",
    "aggregate",
    "dataset_stats",
    "judge_task",
    "load_dataset",
]
