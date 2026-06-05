from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from quantz.candidate import CandidateConfigBuilder
from quantz.compare import RunComparator
from quantz.config import AgentSettings, write_settings
from quantz.memory import JsonlExperienceStore
from quantz.report import ReportBuilder
from quantz.review import ReviewEngine


@dataclass(frozen=True)
class ExperimentPaths:
    root: str
    base_config: str
    base_memory: str
    base_paper_state: str
    base_sim_state: str
    base_report: str
    review: str
    candidate_config: str
    candidate_memory: str
    candidate_paper_state: str
    candidate_sim_state: str
    candidate_report: str
    comparison: str


class ExperimentRunner:
    def __init__(self, monitor_fn: Any) -> None:
        self.monitor_fn = monitor_fn

    def run(self, base_settings: AgentSettings, output_dir: str | Path, iterations: int) -> dict[str, Any]:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        paths = self._paths(root)

        base_run = self._settings_for_run(
            base_settings,
            memory_path=paths.base_memory,
            paper_state_path=paths.base_paper_state,
            sim_state_path=paths.base_sim_state,
        )
        write_settings(base_run, paths.base_config)
        self._run_monitor(base_run, iterations)

        report_builder = ReportBuilder()
        base_report = report_builder.build(JsonlExperienceStore(paths.base_memory).read_raw(), paths.base_paper_state)
        self._write_json(paths.base_report, report_builder.to_dict(base_report))

        review_result = ReviewEngine().review(base_report)
        review_payload = ReviewEngine().to_dict(review_result)
        self._write_json(paths.review, review_payload)

        candidate_base = CandidateConfigBuilder().build(base_settings, review_result)
        candidate_run = self._settings_for_run(
            candidate_base,
            memory_path=paths.candidate_memory,
            paper_state_path=paths.candidate_paper_state,
            sim_state_path=paths.candidate_sim_state,
        )
        self._copy_sim_seed(paths.base_sim_state, paths.candidate_sim_state)
        write_settings(candidate_run, paths.candidate_config)
        self._run_monitor(candidate_run, iterations)

        candidate_report = report_builder.build(
            JsonlExperienceStore(paths.candidate_memory).read_raw(),
            paths.candidate_paper_state,
        )
        self._write_json(paths.candidate_report, report_builder.to_dict(candidate_report))

        comparison = RunComparator().compare(base_report, candidate_report)
        comparison_payload = RunComparator().to_dict(comparison)
        self._write_json(paths.comparison, comparison_payload)

        return {
            "created_at": datetime.now().isoformat(),
            "paths": asdict(paths),
            "review": review_payload,
            "comparison": comparison_payload,
        }

    def _paths(self, root: Path) -> ExperimentPaths:
        return ExperimentPaths(
            root=str(root),
            base_config=str(root / "base-config.json"),
            base_memory=str(root / "base-experience.jsonl"),
            base_paper_state=str(root / "base-paper-state.json"),
            base_sim_state=str(root / "base-sim-market-state.json"),
            base_report=str(root / "base-report.json"),
            review=str(root / "review.json"),
            candidate_config=str(root / "candidate-config.json"),
            candidate_memory=str(root / "candidate-experience.jsonl"),
            candidate_paper_state=str(root / "candidate-paper-state.json"),
            candidate_sim_state=str(root / "candidate-sim-market-state.json"),
            candidate_report=str(root / "candidate-report.json"),
            comparison=str(root / "comparison.json"),
        )

    def _settings_for_run(
        self,
        settings: AgentSettings,
        memory_path: str,
        paper_state_path: str,
        sim_state_path: str,
    ) -> AgentSettings:
        return AgentSettings(
            **{
                **settings.__dict__,
                "mode": "paper",
                "memory_path": memory_path,
                "paper_state_path": paper_state_path,
                "sim_state_path": sim_state_path,
                "interval_seconds": 0.01,
            }
        )

    def _copy_sim_seed(self, source: str, destination: str) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.exists():
            shutil.copyfile(source_path, destination_path)

    def _run_monitor(self, settings: AgentSettings, iterations: int) -> None:
        try:
            self.monitor_fn(settings, iterations, quiet=True)
        except TypeError:
            self.monitor_fn(settings, iterations)

    def _write_json(self, path: str, payload: dict[str, Any]) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
