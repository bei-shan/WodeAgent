"""Shell 命令执行工具 (Bash)

遵循《通用工具响应协议》，返回标准化结构。
在项目根目录沙箱内执行 Shell 命令，支持命令串联与受限 cd。
"""

import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..base import Tool, ToolParameter, ToolStatus, ErrorCode
from core.env import load_env
from core.runtime.cancellation import current_cancel_event
from core.runtime.process_execution import run_process
from core.runtime.command_approval import authorize_bash_command

load_env()


class BashTool(Tool):
    strict_input_contract = True
    """Shell 命令执行工具，支持命令串联与沙箱限制"""
    usage_notes = 'Bash: Execute commands on the selected execution backend. A local process is NOT an OS sandbox; cwd checks and command filters do not confine filesystem/network access. Use for build/test/git/install. Prefer Read/LS/Grep/Glob for inspection. Interactive commands and curl/wget are blocked by default. Chain with &&, ||, ;. Output is bounded.'

    # 默认超时时间（毫秒）
    DEFAULT_TIMEOUT_MS = 120000

    # 最大超时时间（毫秒）
    MAX_TIMEOUT_MS = 600000

    # 输出截断阈值（与 ObservationTruncator 对齐）
    OUTPUT_MAX_LINES = 2000
    OUTPUT_MAX_BYTES = 51200  # 50 KiB; enforced while child is running
    MAX_COMMAND_CHARS = 20000

    # 交互式命令黑名单（直接拒绝）
    INTERACTIVE_COMMANDS: Set[str] = {
        "vim", "vi", "nano", "less", "more", "top", "htop",
        "watch", "tmux", "screen", "ssh", "scp", "sftp", "ftp",
    }
    
    # 破坏性系统命令黑名单
    DESTRUCTIVE_COMMANDS: Set[str] = {
        "mkfs", "fdisk", "dd", "shutdown", "reboot", "poweroff", "halt",
    }
    
    # 权限提升命令黑名单
    PRIVILEGE_COMMANDS: Set[str] = {
        "sudo", "su", "doas",
    }
    
    # 读/搜/列类 Shell 命令黑名单（应使用相应工具）
    READ_SEARCH_COMMANDS: Set[str] = {
        "ls", "cat", "head", "tail", "grep", "find", "rg",
    }

    def __init__(
        self,
        name: str = "Bash",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
    ):
        """
        初始化 Shell 命令执行工具

        Args:
            name: 工具名称，默认为 "Bash"
            project_root: 项目根目录，用于沙箱限制
            working_dir: 工作目录，用于解析相对路径
        """
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")
        
        super().__init__(
            name=name,
            description='Execute a command in a trusted local shell or an explicitly configured sandbox backend. Local cwd and filters are not process isolation. Output is bounded and cancellation is supported.',
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )
        
        # 保存项目根目录
        self._root = self._project_root
        
        # 是否允许网络工具（默认禁用）
        self._allow_network = os.environ.get("BASH_ALLOW_NETWORK", "false").lower() == "true"

    def run(self, parameters: Dict[str, Any]) -> str:
        """
        执行 Shell 命令

        Args:
            parameters: 包含以下键的字典：
                - command: 要执行的命令（必填）
                - directory: 工作目录（相对项目根目录，默认为 "."）
                - timeout_ms: 超时时间（毫秒，默认 120000，最大 600000）

        Returns:
            JSON 格式的响应字符串（遵循《通用工具响应协议》）
        """
        start_time = time.monotonic()
        params_input = dict(parameters)
        # Production fail-closed switch: never silently substitute host Bash
        # when a container/VM sandbox was required by deployment policy.
        if os.environ.get("RIDGE_BASH_REQUIRE_SANDBOX", "").strip().lower() in {
            "1", "true", "yes", "on"
        }:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message="Host Bash disabled: an isolated sandbox backend is required.",
                params_input=params_input,
            )
        
        # 提取参数
        command = parameters.get("command")
        directory = parameters.get("directory", ".")
        timeout_ms = parameters.get("timeout_ms", self.DEFAULT_TIMEOUT_MS)

        # =====================================================================
        # 参数校验
        # =====================================================================
        
        # command 必填
        if not command:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Missing required parameter 'command'.",
                params_input=params_input,
            )
        
        # command 必须是字符串
        if not isinstance(command, str):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'command' must be a string.",
                params_input=params_input,
            )
        
        if len(command) > self.MAX_COMMAND_CHARS:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"Command exceeds {self.MAX_COMMAND_CHARS} characters.",
                params_input=params_input,
            )
        if not isinstance(directory, str) or not directory or "\x00" in directory:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="directory must be a non-empty path string.",
                params_input=params_input,
            )

        # timeout_ms 校验
        if type(timeout_ms) is not int or timeout_ms < 1 or timeout_ms > self.MAX_TIMEOUT_MS:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"timeout_ms must be an integer between 1 and {self.MAX_TIMEOUT_MS}.",
                params_input=params_input,
            )

        # =====================================================================
        # 安全检查：命令黑名单
        # =====================================================================
        
        safety_result = self._check_command_safety(command)
        if safety_result is not None:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=safety_result,
                params_input=params_input,
            )

        # =====================================================================
        # 目录解析与沙箱校验
        # =====================================================================
        
        try:
            # 解析目录路径
            dir_path = Path(directory)
            if dir_path.is_absolute():
                target_dir = dir_path.resolve()
            else:
                target_dir = (self._root / dir_path).resolve()
            
            # 沙箱检查
            target_dir.relative_to(self._root)
            directory_resolved = str(target_dir.relative_to(self._root))
            if not directory_resolved:
                directory_resolved = "."
        except ValueError:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message="Access denied. Path must be within project root.",
                params_input=params_input,
            )
        except OSError as e:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Path resolution failed: {e}",
                params_input=params_input,
            )
        
        # 检查目录是否存在
        if not target_dir.exists():
            return self.create_error_response(
                error_code=ErrorCode.NOT_FOUND,
                message=f"Directory '{directory}' does not exist.",
                params_input=params_input,
            )
        
        # 检查是否为目录
        if not target_dir.is_dir():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"'{directory}' is not a directory.",
                params_input=params_input,
            )

        # =====================================================================
        # 检查命令中的 cd 路径
        # =====================================================================
        
        cd_check_result = self._check_cd_paths(command, target_dir)
        if cd_check_result is not None:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message=cd_check_result,
                params_input=params_input,
            )

        # Command approval is scoped to the exact command, never a cached
        # workspace path decision. The Web session injects the same broker
        # used by PermissionGate; hosted isolation remains a separate guard.
        gate = getattr(self, "_permission_gate", None)
        broker = getattr(gate, "_broker", None) if gate is not None else None
        strict_approval = os.environ.get("RIDGE_BASH_APPROVAL_MODE", "").lower() == "strict"
        if not authorize_bash_command(command, broker, strict=strict_approval):
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message="Command approval denied or unavailable. No process was started.",
                params_input=params_input,
            )

        # =====================================================================
        # 执行命令
        # =====================================================================

        # 设置环境变量
        env = os.environ.copy()
        env["MYCODEAGENT"] = "1"

        # 转换超时时间为秒
        timeout_sec = timeout_ms / 1000.0

        stdout = ""
        stderr = ""
        exit_code = None
        signal_name = None
        timed_out = False

        # 安全策略：简单命令用 shell=False（防注入），复杂命令用 shell=True（已过安全检测）
        exec_command = command
        # The safety parser already accepts uppercase CD (Windows parity).
        # Normalize a leading CD before /bin/sh so the actual execution
        # matches the checked path on POSIX, too.
        if os.name != "nt":
            exec_command = re.sub(r"^(\s*)CD\b", r"\1cd", command, count=1, flags=re.IGNORECASE)
        force_shell = False
        if os.name == "nt":
            exec_command, force_shell = self._translate_windows_shell_compat(command)
        use_shell = force_shell or self._has_shell_operators(exec_command)
        if use_shell:
            run_args = exec_command
        else:
            try:
                run_args = shlex.split(exec_command)
            except ValueError:
                # shlex 无法解析的命令（如不匹配的引号），回退到 shell=True
                use_shell = True
                run_args = exec_command

        try:
            result = run_process(
                run_args, shell=use_shell, cwd=str(target_dir), env=env,
                timeout=timeout_sec, cancel_event=current_cancel_event(),
                max_output_bytes=self.OUTPUT_MAX_BYTES,
            )
            stdout = result.stdout
            stderr = result.stderr
            stdout_total_bytes = result.stdout_bytes
            stderr_total_bytes = result.stderr_bytes
            process_truncated = result.output_truncated
            exit_code = result.returncode
            timed_out = result.timed_out
            if result.interrupted:
                return self.create_error_response(
                    error_code=ErrorCode.INTERRUPTED,
                    message="Command interrupted by user.",
                    params_input=params_input,
                    time_ms=int((time.monotonic() - start_time) * 1000),
                )
        except PermissionError:
            time_ms = int((time.monotonic() - start_time) * 1000)
            return self.create_error_response(
                error_code=ErrorCode.PERMISSION_DENIED,
                message="Permission denied executing command.",
                params_input=params_input,
                time_ms=time_ms,
            )
        except FileNotFoundError:
            time_ms = int((time.monotonic() - start_time) * 1000)
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message=f"Command not found: {run_args[0] if isinstance(run_args, list) else command}",
                params_input=params_input,
                time_ms=time_ms,
            )
        except Exception as e:
            time_ms = int((time.monotonic() - start_time) * 1000)
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message=f"Command failed: {e}",
                params_input=params_input,
                time_ms=time_ms,
            )

        # =====================================================================
        # 构建响应
        # =====================================================================
        
        time_ms = int((time.monotonic() - start_time) * 1000)
        
        # The subprocess runner limits bytes while the child is still
        # writing. Also cap logical lines *before* serializing the result.
        def trim_lines(value: str) -> tuple[str, bool]:
            lines = value.splitlines(keepends=True)
            if len(lines) <= self.OUTPUT_MAX_LINES:
                return value, False
            return "".join(lines[:self.OUTPUT_MAX_LINES]), True

        stdout, stdout_lines_truncated = trim_lines(stdout)
        stderr, stderr_lines_truncated = trim_lines(stderr)

        # 检测输出大小是否超阈值
        stdout_lines = stdout.count("\n") + 1 if stdout else 0
        stdout_bytes = len(stdout.encode("utf-8"))
        stderr_lines = stderr.count("\n") + 1 if stderr else 0
        stderr_bytes = len(stderr.encode("utf-8"))
        output_too_large = (
            process_truncated or stdout_lines_truncated or stderr_lines_truncated
            or             stdout_lines > self.OUTPUT_MAX_LINES
            or stdout_bytes > self.OUTPUT_MAX_BYTES
            or stderr_lines > self.OUTPUT_MAX_LINES
            or stderr_bytes > self.OUTPUT_MAX_BYTES
        )

        # 构建 data 字段
        data: Dict[str, Any] = {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "signal": signal_name,
            "truncated": output_too_large,
            "command": command,
            "directory": directory_resolved,
        }
        
        # 构建 stats 字段
        extra_stats = {
            "stdout_bytes": stdout_total_bytes,
            "stderr_bytes": stderr_total_bytes,
            "stdout_captured_bytes": len(stdout.encode("utf-8")),
            "stderr_captured_bytes": len(stderr.encode("utf-8")),
        }
        
        # 构建 context 字段
        extra_context = {
            "directory_resolved": directory_resolved,
            "cwd": directory_resolved,
        }
        
        # Explicitly explain missing output to the model.
        truncate_notice = "\n[Output truncated: use a narrower command or targeted Read/Grep.]"

        # 构建 text 字段
        if timed_out:
            if stdout or stderr:
                # 超时但有部分输出 -> partial
                text_lines = [
                    f"Command timed out: {command}",
                    f"(Timeout after {timeout_ms}ms)",
                ]
                if stdout:
                    text_lines.append(f"\n--- STDOUT ({len(stdout.encode('utf-8'))} bytes) ---")
                    text_lines.append(stdout[:1000] + ("..." if len(stdout) > 1000 else ""))
                if stderr:
                    text_lines.append(f"\n--- STDERR ({len(stderr.encode('utf-8'))} bytes) ---")
                    text_lines.append(stderr[:1000] + ("..." if len(stderr) > 1000 else ""))
                text = "\n".join(text_lines) + (truncate_notice if output_too_large else "")
                
                return self.create_partial_response(
                    data=data,
                    text=text,
                    params_input=params_input,
                    time_ms=time_ms,
                    extra_stats=extra_stats,
                    extra_context=extra_context,
                )
            else:
                # 超时且无输出 -> error
                return self.create_error_response(
                    error_code=ErrorCode.TIMEOUT,
                    message="Command timed out with no output.",
                    params_input=params_input,
                    time_ms=time_ms,
                )
        
        # 判断状态
        if exit_code == 0:
            # 成功
            text_lines = [
                f"Command succeeded: {command}",
                f"(Exit code 0. Took {time_ms}ms)",
            ]
            if stdout:
                text_lines.append(f"\n--- STDOUT ({len(stdout.encode('utf-8'))} bytes) ---")
                text_lines.append(stdout[:2000] + ("..." if len(stdout) > 2000 else ""))
            if stderr:
                text_lines.append(f"\n--- STDERR ({len(stderr.encode('utf-8'))} bytes) ---")
                text_lines.append(stderr[:1000] + ("..." if len(stderr) > 1000 else ""))
            text = "\n".join(text_lines) + (truncate_notice if output_too_large else "")
            
            return self.create_success_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                extra_context=extra_context,
            )
        else:
            # 非零退出码 -> partial
            text_lines = [
                f"Command failed: {command}",
                f"(Exit code {exit_code}. Took {time_ms}ms)",
            ]
            if stdout:
                text_lines.append(f"\n--- STDOUT ({len(stdout.encode('utf-8'))} bytes) ---")
                text_lines.append(stdout[:2000] + ("..." if len(stdout) > 2000 else ""))
            if stderr:
                text_lines.append(f"\n--- STDERR ({len(stderr.encode('utf-8'))} bytes) ---")
                text_lines.append(stderr[:2000] + ("..." if len(stderr) > 2000 else ""))
            text = "\n".join(text_lines) + (truncate_notice if output_too_large else "")
            
            return self.create_partial_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                extra_context=extra_context,
            )

    def _check_command_safety(self, command: str) -> Optional[str]:
        """
        检查命令安全性

        Args:
            command: 要检查的命令

        Returns:
            如果命令不安全，返回错误消息；否则返回 None
        """
        # ---- 命令替换检测（最高优先级，阻止 shell 注入） ----
        if re.search(r'\$\(|`|\$\{', command):
            return "Command blocked by safety rules. Command substitution ($() / backticks / ${}) is not allowed."

        # ---- 输出重定向到绝对路径 ----
        if re.search(r'[>]\s*/', command):
            return "Command blocked by safety rules. Output redirection to absolute paths is not allowed."

        # 提取命令中的所有"词"（简单分词）
        # 注意：这是一个简化的检查，可能无法捕获所有变体
        # Strip quoted strings to reduce false positives (e.g. echo "ls")
        command_for_scan = re.sub(r'(["\']).*?\1', ' ', command)
        words = re.findall(r'\b\w+\b', command_for_scan.lower())

        # 检查交互式命令
        for word in words:
            if word in self.INTERACTIVE_COMMANDS:
                return f"Command blocked by safety rules. Interactive command '{word}' is not allowed."

        # 检查交互式 git 命令
        if "git" in words:
            if "rebase" in words and ("-i" in command or "--interactive" in command):
                return "Command blocked by safety rules. Interactive 'git rebase -i' is not allowed."
            if "add" in words and ("-i" in command or "--interactive" in command):
                return "Command blocked by safety rules. Interactive 'git add -i' is not allowed."

        # 检查破坏性命令
        for word in words:
            if word in self.DESTRUCTIVE_COMMANDS:
                return f"Command blocked by safety rules. Destructive command '{word}' is not allowed."

        # 检查权限提升命令
        for word in words:
            if word in self.PRIVILEGE_COMMANDS:
                return f"Command blocked by safety rules. Privilege escalation command '{word}' is not allowed."

        # 检查危险的 rm 命令
        if "rm" in words:
            # 检查 rm -rf / 或 rm -rf /*
            if re.search(r'\brm\s+(-[rf]+\s+)*(/|/\*)\s*$', command):
                return "Command blocked by safety rules. Destructive 'rm -rf /' is not allowed."
            if re.search(r'\brm\s+.*-[rf]*\s+.*(/|/\*)', command):
                # 更宽松的检查
                if "/ " in command or "/*" in command:
                    return "Command blocked by safety rules. Destructive 'rm' on root is not allowed."

        # 检查远程脚本执行
        remote_exec_patterns = [
            r'\bcurl\s+.*\|\s*bash',
            r'\bwget\s+.*\|\s*bash',
            r'\bbash\s+<\s*\(\s*curl',
            r'\bbash\s+<\s*\(\s*wget',
            r'\bcurl\s+.*\|\s*sh',
            r'\bwget\s+.*\|\s*sh',
        ]
        for pattern in remote_exec_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return "Command blocked by safety rules. Remote script execution is not allowed."

        # 检查网络工具（默认禁用）
        if not self._allow_network:
            if "curl" in words or "wget" in words:
                return "Command blocked by safety rules. Network tools (curl/wget) are disabled. Set BASH_ALLOW_NETWORK=true to enable."

        # 检查读/搜/列类命令
        for word in words:
            if word in self.READ_SEARCH_COMMANDS:
                tool_suggestion = {
                    "ls": "LS",
                    "cat": "Read",
                    "head": "Read",
                    "tail": "Read",
                    "grep": "Grep",
                    "find": "Glob",
                    "rg": "Grep",
                }.get(word, "the appropriate tool")
                return f"Command blocked by safety rules. Use {tool_suggestion} instead of '{word}'."

        # 检查 Python/Shell 写文件操作
        python_write_patterns = [
            r"\bopen\s*\(.*['\"]w['\"]",   # open(..., 'w')
            r"\bopen\s*\(.*['\"]a['\"]",   # open(..., 'a')
            r"\.write\s*\(",                 # .write()
            r"\.writelines\s*\(",            # .writelines()
            r"pathlib.*\.write_text\s*\(",   # Path.write_text()
            r"shutil\.(copy|move)\s*\(",     # shutil file ops
        ]
        if any(word in {"python", "python3", "py"} for word in words):
            for pat in python_write_patterns:
                if re.search(pat, command):
                    return (
                        "Command blocked by safety rules. "
                        "Python file-write operation detected — use Write/Edit tools instead."
                    )

        return None

    @staticmethod
    def _has_shell_operators(command: str) -> bool:
        """检测命令是否包含需要 shell 解释的操作符。

        如果命令是单个简单命令（无管道、无串联、无重定向），
        应该用 ``shell=False`` + ``shlex.split`` 执行以避免注入风险。
        """
        # 匹配 shell 操作符（不在引号内的）
        # 简化版：检测常见操作符
        return bool(re.search(r'[&|;<>]', re.sub(r'(["\']).*?\1', '', command)))

    @staticmethod
    def _translate_windows_shell_compat(command: str) -> tuple[str, bool]:
        """Translate the tiny POSIX surface this tool promises on Windows."""
        pattern = re.compile(
            r"(?i)(^|(?:&&|\|\||[;&|])\s*)pwd(?=\s*(?:$|&&|\|\||[;&|]))"
        )
        translated = pattern.sub(lambda match: f"{match.group(1)}cd", command)
        return translated, translated != command

    def _check_cd_paths(self, command: str, base_dir: Path) -> Optional[str]:
        """
        检查命令中的 cd 路径是否在项目根目录内（或用户已授权）

        Args:
            command: 要检查的命令
            base_dir: 当前工作目录

        Returns:
            如果 cd 路径越界且用户拒绝/无法确认，返回错误消息；否则返回 None
        """
        # 匹配 cd 命令及其目标路径（大小写不敏感）
        cd_patterns = [
            r'\b[cC][dD]\s+([^\s;&|]+)',  # cd path (case-insensitive)
            r'\b[cC][dD]\s+"([^"]+)"',     # cd "path with spaces"
            r"\b[cC][dD]\s+'([^']+)'",     # cd 'path with spaces'
        ]

        for pattern in cd_patterns:
            for match in re.finditer(pattern, command):
                cd_target = match.group(1)

                # 展开环境变量和 ~
                cd_target = os.path.expandvars(cd_target)
                cd_target = os.path.expanduser(cd_target)

                # 解析 cd 目标路径
                try:
                    if cd_target.startswith("/") or (
                        os.name == "nt" and re.match(r'^[A-Za-z]:', cd_target)
                    ):
                        # 绝对路径
                        resolved = Path(cd_target).resolve()
                    else:
                        # 相对路径（相对于当前工作目录）
                        resolved = (base_dir / cd_target).resolve()

                    # 检查是否在项目根目录内
                    resolved.relative_to(self._root)
                except ValueError:
                    # 软沙箱：弹窗确认
                    gate = getattr(self, "_permission_gate", None)
                    if gate is not None:
                        resolved_str = str(resolved)
                        decision = gate.check(resolved_str)
                        if decision == "granted":
                            continue  # 已授权，放行
                        elif decision == "ask":
                            decision = gate.ask(resolved_str, self.name, "cd")
                            if decision == "granted":
                                continue  # 用户同意，放行
                    return f"Access denied. 'cd {cd_target}' would go outside project root."
                except OSError:
                    # 路径解析失败，继续检查其他 cd
                    pass

        return None

    def get_parameters(self) -> List[ToolParameter]:
        """
        获取工具参数定义
        
        Returns:
            工具参数列表
        """
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The shell command to execute. Command chaining (&&, ||, ;) is allowed.",
                required=True,
            ),
            ToolParameter(
                name="directory",
                type="string",
                description="Working directory relative to project root. Defaults to '.'.",
                required=False,
                default=".",
            ),
            ToolParameter(
                name="timeout_ms",
                type="integer",
                description=f"Execution timeout in milliseconds. Default {self.DEFAULT_TIMEOUT_MS}, max {self.MAX_TIMEOUT_MS}.",
                required=False,
                default=self.DEFAULT_TIMEOUT_MS,
            ),
        ]
