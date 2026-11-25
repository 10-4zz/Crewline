"""Agent capability definitions and display labels."""

from enum import StrEnum
from typing import Iterable


class AgentCapability(StrEnum):
    """Capabilities an agent adapter can provide to the user."""

    ANALYSIS = "analysis"
    EDIT_FILES = "edit_files"
    RUN_TESTS = "run_tests"

    @property
    def label_zh(self) -> str:
        return _CAPABILITY_LABELS_ZH[self]


_CAPABILITY_LABELS_ZH: dict[AgentCapability, str] = {
    AgentCapability.ANALYSIS: "输出分析结果",
    AgentCapability.EDIT_FILES: "修改工作区文件",
    AgentCapability.RUN_TESTS: "运行测试",
}


def capability_labels(capabilities: Iterable[AgentCapability]) -> list[str]:
    """Return human-readable Chinese labels for capabilities."""
    return [cap.label_zh for cap in capabilities]
