"""异常体系"""

class HelloAgentsException(Exception):
    """HelloAgents基础异常类"""
    pass

class LLMException(HelloAgentsException):
    """LLM相关异常"""
    pass

class AgentException(HelloAgentsException):
    """Agent相关异常"""
    pass

class ConfigException(HelloAgentsException):
    """配置相关异常"""
    pass

class ToolException(HelloAgentsException):
    """工具相关异常"""
    pass

class BudgetExceeded(AgentException):
    """Token budget exceeded — only raised when BUDGET_ENFORCE=true.

    Carries the spent/total counts so the caller can render a useful
    message. The BudgetFeature.pre_tool_use hook short-circuits tool
    execution with a structured ``blocked`` response rather than raising
    this exception; the exception is reserved for callers that opt into
    "fail loud" behavior (e.g. external orchestrators driving CodeAgent
    programmatically).
    """

    def __init__(self, spent: int, total: int):
        self.spent = spent
        self.total = total
        super().__init__(
            f"Token budget exceeded: spent {spent:,} / total {total:,} "
            f"({(spent / total) * 100:.0f}%)"
        )
