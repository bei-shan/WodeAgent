switch_model_prompt = """## SwitchModel

Switch the active LLM model during the conversation.

Parameters:
- model: (required) Model identifier to switch to.

Use this when:
- A task requires a different model's strengths (e.g. reasoning vs speed).
- The user asks to switch models.

Examples:
- SwitchModel(model="gpt-4o")
- SwitchModel(model="deepseek-v3")
- SwitchModel(model="claude-sonnet-4-6")
"""
